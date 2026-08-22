"""
Driver Pickup Reminder Agent (v1)

Runs once per minute from cron / Task Scheduler. Each run is a complete,
independent sweep: read the rides sheet, find rides whose pickup is within the
next 30 minutes and that have not been reminded yet, place a Twilio voice call
to the driver, and write the outcome back to the sheet.

All state lives in the sheet. Nothing is remembered between runs, so a crash,
a reboot or a laptop going to sleep costs at most one tick.

Usage:
    python reminder.py                              # normal run
    python reminder.py --dry-run                    # no real calls
    python reminder.py --now "2026-08-21 08:35"     # fake clock, for testing
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv()

SHEET_ID = os.getenv("SHEET_ID", "")
SHEET_TAB = os.getenv("SHEET_TAB", "Rides")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
LEAD_MINUTES = int(os.getenv("LEAD_MINUTES", "30"))
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "+91")

# Twilio trial accounts can only dial numbers you have verified. Set this to
# your own verified number and every call is redirected there for testing.
TEST_OVERRIDE_NUMBER = os.getenv("TEST_OVERRIDE_NUMBER", "").strip()

# Where the call gets its spoken script from.
#
# Preferred: pass the TwiML inline on calls.create() - no hosting at all.
# But Twilio *trial* accounts reject the inline `twiml` parameter (HTTP 400,
# "trial accounts have limited parameter access"), so on a trial we point the
# call at a TwiML Bin instead - still Twilio-hosted, still no server of ours -
# and pass the spoken text as the `msg` template variable.
#
# Set TWIML_URL to the Bin's URL to use that path; leave it blank on an
# upgraded account to use inline TwiML.
TWIML_URL = os.getenv("TWIML_URL", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")

# How long to wait for a call to reach a final state before giving up on the
# answer. The call itself is unaffected; we just stop watching.
CALL_POLL_SECONDS = int(os.getenv("CALL_POLL_SECONDS", "60"))
CALL_POLL_INTERVAL = 3

LOG_FILE = os.getenv("LOG_FILE", "reminders.log")

# Only one sweep may run at a time. Waiting for a call to finish can take up to
# CALL_POLL_SECONDS, so a tick can outlive the one-minute cron interval; two
# overlapping ticks would both read a blank status and both dial the same
# driver. Claiming the row guards sequential re-runs, not concurrent ones.
LOCK_FILE = os.getenv("LOCK_FILE", "reminder.lock")
LOCK_STALE_SECONDS = 900

TZ = ZoneInfo(TIMEZONE)

# --------------------------------------------------------------------------
# Sheet layout
# --------------------------------------------------------------------------

# Columns A-D are the operator's own data. Columns E-H belong to this agent
# and must exist before the first run (see README setup).
COL_STATUS = "Reminder Status"
COL_SID = "Call SID"
COL_RESULT = "Call Result"
COL_UPDATED = "Last Updated"
AGENT_HEADERS = [COL_STATUS, COL_SID, COL_RESULT, COL_UPDATED]

FIRST_DATA_ROW = 2

# Reminder Status values
STATUS_CALLING = "CALLING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"
STATUS_TOO_LATE = "SKIPPED_TOO_LATE"
STATUS_BAD_DATA = "SKIPPED_BAD_DATA"

TERMINAL_CALL_STATES = {"completed", "no-answer", "busy", "failed", "canceled"}
ANSWERED_STATES = {"completed"}

log = logging.getLogger("reminder")


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)
    try:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(fmt)
        log.addHandler(file_handler)
    except OSError as exc:  # read-only dir, etc. Not worth failing the run.
        log.warning("could not open log file %s: %s", LOG_FILE, exc)


@contextlib.contextmanager
def single_instance(path: str = LOCK_FILE, stale_after: int = LOCK_STALE_SECONDS):
    """Yield True if this process got the lock, False if a sweep is running.

    A lock left behind by a killed process is reclaimed once it goes stale, so
    a crash can never wedge the agent permanently.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            age = 0.0
        if age < stale_after:
            yield False
            return
        log.warning("reclaiming stale lock %s (%.0f s old)", path, age)
        with contextlib.suppress(OSError):
            os.unlink(path)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            yield False
            return
    try:
        with contextlib.suppress(OSError):
            os.write(fd, str(os.getpid()).encode())
        yield True
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)


# --------------------------------------------------------------------------
# Parsing helpers (pure functions - these are what the unit tests cover)
# --------------------------------------------------------------------------


def normalize_phone(raw: str, default_cc: str = DEFAULT_COUNTRY_CODE) -> str | None:
    """Turn sheet phone text into E.164, or None if it cannot be trusted.

    The sample sheet uses '+91 98765 43210'; Twilio rejects anything that is
    not E.164, so this runs on every row before dialling.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    has_plus = text.lstrip().startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None

    if has_plus:
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if len(digits) == 10:
        return f"{default_cc}{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"{default_cc}{digits[1:]}"
    if 11 <= len(digits) <= 15:
        return "+" + digits
    return None


def parse_pickup_time(raw: str, tz: ZoneInfo = TZ) -> datetime | None:
    """Parse the sheet's pickup time and pin it to the fleet's timezone.

    Sheet values are timezone-naive. The server may not be in IST, so the
    timezone is always applied from config and never inferred.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = date_parser.parse(text, dayfirst=True)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def build_message(name: str, location: str, pickup: datetime, lead: int) -> str:
    """The words the driver hears."""
    when = pickup.strftime("%I:%M %p").lstrip("0")
    who = (name or "driver").strip()
    return (
        f"Hello {who}. This is an automated reminder from fleet operations. "
        f"You have a pickup at {location} at {when}, in about {lead} minutes. "
        "Please call the customer now to confirm, and start heading to the "
        "pickup location."
    )


def build_twiml(message: str) -> str:
    """Say the message, pause, say it once more.

    Drivers routinely answer mid-sentence, so a single pass gets half heard.
    """
    safe = xml_escape(message)
    return (
        "<Response>"
        f'<Say voice="alice" language="en-IN">{safe}</Say>'
        '<Pause length="1"/>'
        f'<Say voice="alice" language="en-IN">{safe}</Say>'
        "</Response>"
    )


def build_call_url(base: str, message: str) -> str:
    """TwiML Bin URL with the spoken text as the `msg` template variable."""
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode({'msg': message})}"


# --------------------------------------------------------------------------
# Sheet access
# --------------------------------------------------------------------------


@dataclass
class Ride:
    row_number: int
    driver_name: str
    driver_phone: str
    pickup_location: str
    pickup_time_raw: str
    status: str
    call_sid: str
    call_result: str
    last_updated: str


class ConfigError(RuntimeError):
    """Setup is incomplete - retrying will not help, a human must fix it."""


class HeaderError(ConfigError):
    """The sheet is not set up for the agent yet."""


class RideSheet:
    """Thin wrapper over one worksheet. Reads all rows, writes columns E-H."""

    def __init__(self, worksheet):
        self._ws = worksheet

    def _values(self) -> list[list[str]]:
        return self._ws.get_all_values()

    def load(self) -> list[Ride]:
        values = self._values()
        if not values:
            raise HeaderError("Sheet is empty - expected a header row.")

        header = [h.strip() for h in values[0]]
        missing = [h for h in AGENT_HEADERS if h not in header[4:8]]
        if len(header) < 8 or missing:
            raise HeaderError(
                "Sheet is missing the agent columns. Add these headers to the "
                f"first row, in columns E, F, G, H exactly: {AGENT_HEADERS}. "
                f"Found: {header}"
            )

        rides = []
        for offset, row in enumerate(values[1:]):
            padded = list(row) + [""] * (8 - len(row))
            if not any(str(c).strip() for c in padded[:4]):
                continue  # blank spacer row
            rides.append(
                Ride(
                    row_number=FIRST_DATA_ROW + offset,
                    driver_name=padded[0].strip(),
                    driver_phone=padded[1].strip(),
                    pickup_location=padded[2].strip(),
                    pickup_time_raw=padded[3].strip(),
                    status=padded[4].strip(),
                    call_sid=padded[5].strip(),
                    call_result=padded[6].strip(),
                    last_updated=padded[7].strip(),
                )
            )
        return rides

    def update(
        self,
        ride: Ride,
        status: str | None = None,
        call_sid: str | None = None,
        call_result: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Write columns E-H for one row in a single API call."""
        if status is not None:
            ride.status = status
        if call_sid is not None:
            ride.call_sid = call_sid
        if call_result is not None:
            ride.call_result = call_result
        stamp = (now or datetime.now(TZ)).strftime("%Y-%m-%d %H:%M:%S %Z")
        ride.last_updated = stamp

        self._ws.update(
            range_name=f"E{ride.row_number}:H{ride.row_number}",
            values=[[ride.status, ride.call_sid, ride.call_result, ride.last_updated]],
        )


def open_sheet() -> RideSheet:
    import gspread

    if not SHEET_ID:
        raise ConfigError("SHEET_ID is not set. Copy .env.example to .env.")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise ConfigError(
            f"Service account key not found at '{SERVICE_ACCOUNT_FILE}'. "
            "See the setup steps in README.md."
        )
    client = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    spreadsheet = client.open_by_key(SHEET_ID)
    return RideSheet(spreadsheet.worksheet(SHEET_TAB))


# --------------------------------------------------------------------------
# Calling
# --------------------------------------------------------------------------


@dataclass
class CallOutcome:
    sid: str
    result: str  # twilio status, 'dry-run', 'unknown-timeout', or 'error: ...'
    answered: bool


class DryRunCaller:
    """Does everything except dial. Sheet still gets written."""

    def place_and_wait(self, to: str, message: str) -> CallOutcome:
        log.info("DRY_RUN would call %s with: %s", to, message)
        return CallOutcome(sid="DRYRUN", result="dry-run", answered=True)


class TwilioCaller:
    """Places the call, then waits for it to reach a final state.

    Either way (inline TwiML or a TwiML Bin URL) the call script is hosted by
    Twilio, so this project never runs a web server or a tunnel. See TWIML_URL.
    """

    def __init__(self, client, from_number: str):
        self._client = client
        self._from = from_number

    def place_and_wait(self, to: str, message: str) -> CallOutcome:
        kwargs = {"to": to, "from_": self._from}
        if TWIML_URL:
            # NB: do not pass `method` here - Twilio trial accounts reject it
            # outright ("limited parameter access"). The default (POST) is
            # fine; the `msg` value rides in the query string either way.
            kwargs["url"] = build_call_url(TWIML_URL, message)
        else:
            kwargs["twiml"] = build_twiml(message)
        call = self._client.calls.create(**kwargs)
        sid = call.sid
        log.info("call placed sid=%s to=%s", sid, to)

        deadline = time.monotonic() + CALL_POLL_SECONDS
        while True:
            fetched = self._client.calls(sid).fetch()
            status = fetched.status
            if status in TERMINAL_CALL_STATES:
                return CallOutcome(
                    sid=sid, result=status, answered=status in ANSWERED_STATES
                )
            if time.monotonic() >= deadline:
                # The call went out; we simply stopped watching it.
                return CallOutcome(sid=sid, result="unknown-timeout", answered=True)
            time.sleep(CALL_POLL_INTERVAL)


def build_caller(dry_run: bool):
    if dry_run:
        return DryRunCaller()
    from twilio.rest import Client

    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
            ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
            ("TWILIO_FROM_NUMBER", TWILIO_FROM_NUMBER),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"Missing Twilio settings in .env: {', '.join(missing)}")
    return TwilioCaller(Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), TWILIO_FROM_NUMBER)


def explain_twilio_error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    text = str(exc).lower()
    if code in (21219, 573002) or "not verified" in text or "verified recipient" in text:
        return (
            "error: number not verified on Twilio trial. Trial accounts can only "
            "call numbers verified in the Twilio console - verify it there, or "
            "set TEST_OVERRIDE_NUMBER in .env to a number you have verified"
        )
    if "limited parameter access" in text:
        return (
            "error: trial account rejected a call parameter. Inline TwiML is not "
            "allowed on trial - create a TwiML Bin and set TWIML_URL in .env "
            "(see README)"
        )
    if code == 21606 or code == 21210:
        return "error: TWILIO_FROM_NUMBER is not a valid Twilio caller ID"
    if code:
        return f"error: twilio {code}: {exc}"
    return f"error: {exc}"


# --------------------------------------------------------------------------
# One sweep
# --------------------------------------------------------------------------


def run_tick(sheet: RideSheet, caller, now: datetime, lead_minutes: int = LEAD_MINUTES) -> dict:
    """Process every ride once. Returns a small summary for logging/tests."""
    summary = {"scanned": 0, "called": 0, "sent": 0, "failed": 0, "skipped": 0}

    rides = sheet.load()
    summary["scanned"] = len(rides)

    for ride in rides:
        # Any non-blank status means this row is already accounted for.
        # This is the read half of the duplicate guard.
        if ride.status:
            continue

        pickup = parse_pickup_time(ride.pickup_time_raw)
        phone = normalize_phone(ride.driver_phone)

        if pickup is None or phone is None:
            reason = []
            if pickup is None:
                reason.append(f"unparseable time '{ride.pickup_time_raw}'")
            if phone is None:
                reason.append(f"unusable phone '{ride.driver_phone}'")
            detail = "; ".join(reason)
            log.warning("row %s skipped: %s", ride.row_number, detail)
            sheet.update(ride, status=STATUS_BAD_DATA, call_result=detail, now=now)
            summary["skipped"] += 1
            continue

        minutes_until = (pickup - now).total_seconds() / 60.0

        if minutes_until <= 0:
            log.info(
                "row %s skipped: pickup was %.0f min ago", ride.row_number, -minutes_until
            )
            sheet.update(
                ride,
                status=STATUS_TOO_LATE,
                call_result=f"pickup {abs(minutes_until):.0f} min in the past at scan time",
                now=now,
            )
            summary["skipped"] += 1
            continue

        if minutes_until > lead_minutes:
            continue  # not due yet; a later tick will pick it up

        # --- Claim the row BEFORE dialling. ---
        # If we crash between here and the call, the row stays CALLING and is
        # never dialled again. A missed reminder is cheaper than calling a
        # driver twice.
        sheet.update(ride, status=STATUS_CALLING, call_result="", now=now)

        target = TEST_OVERRIDE_NUMBER or phone
        if TEST_OVERRIDE_NUMBER:
            log.info(
                "row %s: overriding %s -> %s (test mode)",
                ride.row_number,
                phone,
                TEST_OVERRIDE_NUMBER,
            )

        message = build_message(
            ride.driver_name, ride.pickup_location, pickup, round(minutes_until)
        )

        try:
            outcome = caller.place_and_wait(target, message)
        except Exception as exc:  # noqa: BLE001 - close the row, keep sweeping
            detail = explain_twilio_error(exc)
            log.error("row %s call failed: %s", ride.row_number, detail)
            sheet.update(ride, status=STATUS_FAILED, call_result=detail, now=now)
            summary["failed"] += 1
            continue

        summary["called"] += 1
        final_status = STATUS_SENT if outcome.answered else STATUS_FAILED
        sheet.update(
            ride,
            status=final_status,
            call_sid=outcome.sid,
            call_result=outcome.result,
            now=now,
        )
        if final_status == STATUS_SENT:
            summary["sent"] += 1
        else:
            summary["failed"] += 1

        log.info(
            "row %s driver=%s pickup=%s in %.0f min -> %s (%s) sid=%s",
            ride.row_number,
            ride.driver_name,
            pickup.strftime("%Y-%m-%d %H:%M"),
            minutes_until,
            final_status,
            outcome.result,
            outcome.sid,
        )

    return summary


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Driver pickup reminder agent (v1)")
    ap.add_argument(
        "--now",
        help='Pretend it is this local time, e.g. "2026-08-21 08:35". For testing.',
    )
    ap.add_argument("--dry-run", action="store_true", help="Never place a real call.")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    setup_logging()
    args = parse_args(argv)

    now = parse_pickup_time(args.now) if args.now else datetime.now(TZ)
    if now is None:
        log.error("could not understand --now value %r", args.now)
        return 2

    dry_run = DRY_RUN or args.dry_run

    # One try/except around the whole tick. If anything transient goes wrong,
    # the next run is 60 seconds away - that is the retry.
    try:
        with single_instance() as acquired:
            if not acquired:
                log.info("previous sweep still running; skipping this tick")
                return 0
            sheet = open_sheet()
            caller = build_caller(dry_run)
            summary = run_tick(sheet, caller, now)
    except ConfigError as exc:
        # Setup problem: exit 2 and stay quiet on the next tick until fixed.
        log.error("setup incomplete: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        log.error("tick failed (%s); next run will retry: %s", type(exc).__name__, exc)
        return 1

    log.info(
        "tick complete at %s dry_run=%s %s",
        now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        dry_run,
        summary,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
