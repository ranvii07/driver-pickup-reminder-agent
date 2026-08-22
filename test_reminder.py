"""Tests for the reminder agent.

Two groups:
  1. The fiddly pure functions - phone and time parsing.
  2. The sweep itself, driven against an in-memory sheet and a fake caller,
     so the whole decision logic can be exercised without Google or Twilio.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import reminder
from reminder import (
    STATUS_BAD_DATA,
    STATUS_CALLING,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_TOO_LATE,
    CallOutcome,
    Ride,
    build_message,
    build_twiml,
    normalize_phone,
    parse_pickup_time,
    run_tick,
)

IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------
# Phone normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+91 98765 43210", "+919876543210"),  # exact format in the sample sheet
        ("+91 91234 56780", "+919123456780"),
        ("9876543210", "+919876543210"),  # bare 10 digit
        ("09876543210", "+919876543210"),  # leading zero
        ("+919876543210", "+919876543210"),  # already E.164
        ("919876543210", "+919876543210"),  # country code, no plus
        ("+1 415 555 2671", "+14155552671"),  # non-India
        ("98765-43210", "+919876543210"),
        ("  9876543210  ", "+919876543210"),
    ],
)
def test_normalize_phone_valid(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "not a phone", "12345", "+", "abc123"])
def test_normalize_phone_rejects_garbage(raw):
    assert normalize_phone(raw) is None


# --------------------------------------------------------------------------
# Time parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2026-08-21 09:00:00",
        "2026-08-21 09:00",
        "8/21/2026 9:00:00",
        "21/08/2026 09:00",
        "21-08-2026 9:00 AM",
    ],
)
def test_parse_pickup_time_formats(raw):
    parsed = parse_pickup_time(raw)
    assert parsed == datetime(2026, 8, 21, 9, 0, tzinfo=IST)


def test_parse_pickup_time_always_applies_configured_timezone():
    """Sheet values are naive; the timezone must come from config, not the OS."""
    parsed = parse_pickup_time("2026-08-21 09:00:00")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 5.5 * 3600


@pytest.mark.parametrize("raw", ["", None, "sometime tomorrow", "not a date"])
def test_parse_pickup_time_rejects_garbage(raw):
    assert parse_pickup_time(raw) is None


# --------------------------------------------------------------------------
# Message / TwiML
# --------------------------------------------------------------------------


def test_message_contains_the_operational_facts():
    msg = build_message(
        "Ramesh Kumar", "DLF Cyber City, Gurugram", parse_pickup_time("2026-08-21 09:00"), 30
    )
    assert "Ramesh Kumar" in msg
    assert "DLF Cyber City, Gurugram" in msg
    assert "9:00 AM" in msg
    assert "30 minutes" in msg
    assert "call the customer" in msg.lower()


def test_twiml_says_it_twice_and_escapes_xml():
    twiml = build_twiml("Pickup at Tom & Jerry's <Cafe>")
    assert twiml.count("<Say") == 2
    assert "&amp;" in twiml and "&lt;Cafe&gt;" in twiml
    assert "<Cafe>" not in twiml


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeSheet:
    """In-memory stand-in for the worksheet, with the same read/write surface."""

    def __init__(self, rows):
        # rows: list of (name, phone, location, time_raw)
        self.rides = [
            Ride(
                row_number=2 + i,
                driver_name=r[0],
                driver_phone=r[1],
                pickup_location=r[2],
                pickup_time_raw=r[3],
                status="",
                call_sid="",
                call_result="",
                last_updated="",
            )
            for i, r in enumerate(rows)
        ]
        self.writes = []

    def load(self):
        return list(self.rides)

    def update(self, ride, status=None, call_sid=None, call_result=None, now=None):
        stored = next(r for r in self.rides if r.row_number == ride.row_number)
        if status is not None:
            stored.status = ride.status = status
        if call_sid is not None:
            stored.call_sid = ride.call_sid = call_sid
        if call_result is not None:
            stored.call_result = ride.call_result = call_result
        stamp = (now or datetime.now(IST)).strftime("%Y-%m-%d %H:%M:%S")
        stored.last_updated = ride.last_updated = stamp
        self.writes.append((ride.row_number, stored.status, stored.call_result))

    def row(self, n):
        return next(r for r in self.rides if r.row_number == n)


class FakeCaller:
    def __init__(self, result="completed", answered=True, raises=None):
        self.calls = []
        self._result = result
        self._answered = answered
        self._raises = raises

    def place_and_wait(self, to, message):
        self.calls.append((to, message))
        if self._raises:
            raise self._raises
        return CallOutcome(
            sid=f"CA{len(self.calls):032d}", result=self._result, answered=self._answered
        )


SAMPLE_ROWS = [
    ("Ramesh Kumar", "+91 98765 43210", "DLF Cyber City, Gurugram", "2026-08-21 09:00"),
    ("Suresh Yadav", "+91 91234 56780", "Indira Gandhi Intl Airport, T3", "2026-08-21 09:45"),
    ("Amit Sharma", "+91 99887 66554", "Cyberhub, Sector 24, Gurugram", "2026-08-21 10:30"),
]


def at(text):
    return parse_pickup_time(text)


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------


def test_calls_only_the_ride_inside_the_window():
    sheet = FakeSheet(SAMPLE_ROWS)
    caller = FakeCaller()

    # 08:35 -> Ramesh (09:00) is 25 min out. The other two are >30 min out.
    run_tick(sheet, caller, at("2026-08-21 08:35"))

    assert len(caller.calls) == 1
    assert "Ramesh Kumar" in caller.calls[0][1]
    assert sheet.row(2).status == STATUS_SENT
    assert sheet.row(3).status == ""  # untouched, not yet due
    assert sheet.row(4).status == ""


def test_ride_not_yet_due_is_left_completely_alone():
    sheet = FakeSheet(SAMPLE_ROWS)
    caller = FakeCaller()
    run_tick(sheet, caller, at("2026-08-21 07:00"))
    assert caller.calls == []
    assert all(r.status == "" for r in sheet.rides)


def test_no_duplicate_when_the_same_tick_runs_five_times():
    """The core guarantee: cron overlap, retries, reboots - still one call."""
    sheet = FakeSheet(SAMPLE_ROWS)
    caller = FakeCaller()

    for _ in range(5):
        run_tick(sheet, caller, at("2026-08-21 08:35"))

    assert len(caller.calls) == 1
    assert sheet.row(2).status == STATUS_SENT


def test_no_duplicate_across_advancing_ticks():
    sheet = FakeSheet(SAMPLE_ROWS)
    caller = FakeCaller()

    for minute in range(35, 60):  # 08:35 .. 08:59, every minute
        run_tick(sheet, caller, at(f"2026-08-21 08:{minute:02d}"))

    assert len(caller.calls) == 1


def test_row_is_claimed_before_the_call_is_placed():
    """If we crash mid-dial the row must already read CALLING."""
    seen = {}

    class SpyCaller(FakeCaller):
        def place_and_wait(self, to, message):
            seen["status_at_dial_time"] = sheet.row(2).status
            return super().place_and_wait(to, message)

    sheet = FakeSheet(SAMPLE_ROWS)
    run_tick(sheet, SpyCaller(), at("2026-08-21 08:35"))
    assert seen["status_at_dial_time"] == STATUS_CALLING


def test_crash_during_call_leaves_row_claimed_and_never_redials():
    sheet = FakeSheet(SAMPLE_ROWS)

    class Exploding(FakeCaller):
        def place_and_wait(self, to, message):
            self.calls.append((to, message))
            raise KeyboardInterrupt("simulated crash mid-dial")

    boom = Exploding()
    with pytest.raises(KeyboardInterrupt):
        run_tick(sheet, boom, at("2026-08-21 08:35"))
    assert sheet.row(2).status == STATUS_CALLING

    # Next tick must not dial it again.
    later = FakeCaller()
    run_tick(sheet, later, at("2026-08-21 08:36"))
    assert later.calls == []


def test_unanswered_call_is_logged_as_failed():
    sheet = FakeSheet(SAMPLE_ROWS)
    caller = FakeCaller(result="no-answer", answered=False)
    run_tick(sheet, caller, at("2026-08-21 08:35"))
    assert sheet.row(2).status == STATUS_FAILED
    assert sheet.row(2).call_result == "no-answer"


def test_twilio_error_closes_the_row_instead_of_leaving_it_stuck():
    sheet = FakeSheet(SAMPLE_ROWS)
    caller = FakeCaller(raises=RuntimeError("boom"))
    run_tick(sheet, caller, at("2026-08-21 08:35"))
    assert sheet.row(2).status == STATUS_FAILED
    assert "boom" in sheet.row(2).call_result


def test_past_pickup_is_skipped_not_called():
    sheet = FakeSheet(SAMPLE_ROWS)
    caller = FakeCaller()
    run_tick(sheet, caller, at("2026-08-21 09:30"))  # Ramesh 09:00 already gone
    assert sheet.row(2).status == STATUS_TOO_LATE
    # Suresh at 09:45 is 15 min out -> due
    assert sheet.row(3).status == STATUS_SENT
    assert len(caller.calls) == 1


def test_catch_up_still_calls_a_ride_that_became_due_while_we_were_down():
    """Down from 08:30, first tick at 08:52: Ramesh (09:00) is 8 min out."""
    sheet = FakeSheet(SAMPLE_ROWS)
    caller = FakeCaller()
    run_tick(sheet, caller, at("2026-08-21 08:52"))
    assert len(caller.calls) == 1
    assert sheet.row(2).status == STATUS_SENT


def test_bad_rows_are_marked_and_never_dialled():
    sheet = FakeSheet(
        [
            ("No Phone", "not a phone", "Sector 62, Noida", "2026-08-21 09:00"),
            ("No Time", "+91 98765 43210", "Sector 62, Noida", "whenever"),
        ]
    )
    caller = FakeCaller()
    run_tick(sheet, caller, at("2026-08-21 08:35"))
    assert caller.calls == []
    assert sheet.row(2).status == STATUS_BAD_DATA
    assert "unusable phone" in sheet.row(2).call_result
    assert sheet.row(3).status == STATUS_BAD_DATA
    assert "unparseable time" in sheet.row(3).call_result


def test_test_override_number_redirects_every_call(monkeypatch):
    monkeypatch.setattr(reminder, "TEST_OVERRIDE_NUMBER", "+919000000000")
    sheet = FakeSheet(SAMPLE_ROWS)
    caller = FakeCaller()
    run_tick(sheet, caller, at("2026-08-21 08:35"))
    assert caller.calls[0][0] == "+919000000000"


def test_dry_run_writes_the_sheet_but_places_no_call():
    sheet = FakeSheet(SAMPLE_ROWS)
    run_tick(sheet, reminder.DryRunCaller(), at("2026-08-21 08:35"))
    assert sheet.row(2).status == STATUS_SENT
    assert sheet.row(2).call_result == "dry-run"


def test_clearing_the_status_re_arms_a_row():
    """Documented operator escape hatch: blank the cell to allow a re-call."""
    sheet = FakeSheet(SAMPLE_ROWS)
    caller = FakeCaller()
    run_tick(sheet, caller, at("2026-08-21 08:35"))
    assert len(caller.calls) == 1

    sheet.row(2).status = ""
    run_tick(sheet, caller, at("2026-08-21 08:40"))
    assert len(caller.calls) == 2


def test_summary_counts():
    sheet = FakeSheet(SAMPLE_ROWS)
    summary = run_tick(sheet, FakeCaller(), at("2026-08-21 08:35"))
    assert summary == {"scanned": 3, "called": 1, "sent": 1, "failed": 0, "skipped": 0}


# --------------------------------------------------------------------------
# RideSheet against a stand-in worksheet (same surface gspread exposes)
# --------------------------------------------------------------------------

HEADER = [
    "Driver Name",
    "Driver Phone Number",
    "Pickup Location",
    "Scheduled Pickup Time",
    "Reminder Status",
    "Call SID",
    "Call Result",
    "Last Updated",
]


class FakeWorksheet:
    def __init__(self, values):
        self.values = [list(r) for r in values]
        self.updates = []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def update(self, range_name, values):
        self.updates.append((range_name, values))


def test_ridesheet_loads_and_pads_short_rows():
    ws = FakeWorksheet(
        [
            HEADER,
            ["Ramesh Kumar", "+91 98765 43210", "DLF Cyber City", "2026-08-21 09:00"],
            ["", "", "", "", "", "", "", ""],  # blank spacer row is ignored
            ["Suresh Yadav", "+91 91234 56780", "IGI T3", "2026-08-21 09:45", "SENT"],
        ]
    )
    rides = reminder.RideSheet(ws).load()
    assert len(rides) == 2
    assert rides[0].row_number == 2 and rides[0].status == ""
    assert rides[1].row_number == 4 and rides[1].status == "SENT"


def test_ridesheet_rejects_a_sheet_without_the_agent_columns():
    ws = FakeWorksheet([HEADER[:4], ["A", "+919876543210", "X", "2026-08-21 09:00"]])
    with pytest.raises(reminder.HeaderError) as exc:
        reminder.RideSheet(ws).load()
    assert "columns E, F, G, H" in str(exc.value)


def test_ridesheet_writes_one_a1_range_per_row():
    ws = FakeWorksheet(
        [HEADER, ["Ramesh", "+91 98765 43210", "DLF", "2026-08-21 09:00"]]
    )
    sheet = reminder.RideSheet(ws)
    ride = sheet.load()[0]
    sheet.update(ride, status="SENT", call_sid="CA123", call_result="completed",
                 now=at("2026-08-21 08:35"))
    assert len(ws.updates) == 1
    range_name, values = ws.updates[0]
    assert range_name == "E2:H2"
    assert values[0][:3] == ["SENT", "CA123", "completed"]
    assert values[0][3].startswith("2026-08-21 08:35")


def test_full_sweep_through_the_real_sheet_layer():
    """End-to-end through RideSheet: claim write, then result write."""
    ws = FakeWorksheet(
        [HEADER, ["Ramesh Kumar", "+91 98765 43210", "DLF Cyber City", "2026-08-21 09:00"]]
    )
    sheet = reminder.RideSheet(ws)
    caller = FakeCaller()
    run_tick(sheet, caller, at("2026-08-21 08:35"))

    assert [u[1][0][0] for u in ws.updates] == [STATUS_CALLING, STATUS_SENT]

    # Re-read from the sheet's own data and sweep again: no second call.
    ws.values[1] = ws.values[1][:4] + ws.updates[-1][1][0]
    run_tick(reminder.RideSheet(ws), caller, at("2026-08-21 08:36"))
    assert len(caller.calls) == 1


# --------------------------------------------------------------------------
# TwilioCaller polling loop
# --------------------------------------------------------------------------


class FakeTwilioCall:
    def __init__(self, sid, status):
        self.sid = sid
        self.status = status


class FakeTwilioClient:
    """Returns a scripted sequence of statuses from successive fetches."""

    def __init__(self, statuses):
        # created entries record exactly the kwargs passed to calls.create
        self._statuses = list(statuses)
        self.created = []
        self.fetches = 0
        client = self

        class Calls:
            def create(self, **kw):
                client.created.append(kw)
                return FakeTwilioCall("CA_TEST", "queued")

            def __call__(self, sid):
                class Fetcher:
                    def fetch(_self):
                        client.fetches += 1
                        idx = min(client.fetches - 1, len(client._statuses) - 1)
                        return FakeTwilioCall(sid, client._statuses[idx])

                return Fetcher()

        self.calls = Calls()


@pytest.fixture(autouse=True)
def _isolate_from_local_env(monkeypatch):
    """Tests must not depend on whatever happens to be in the developer's .env."""
    monkeypatch.setattr(reminder, "CALL_POLL_INTERVAL", 0)
    monkeypatch.setattr(reminder, "TWIML_URL", "")
    monkeypatch.setattr(reminder, "TEST_OVERRIDE_NUMBER", "")


def test_twilio_caller_waits_for_a_terminal_status():
    client = FakeTwilioClient(["queued", "ringing", "in-progress", "completed"])
    outcome = reminder.TwilioCaller(client, "+15005550006").place_and_wait(
        "+919876543210", "hello"
    )
    assert outcome.result == "completed" and outcome.answered is True
    assert outcome.sid == "CA_TEST"
    assert client.fetches == 4


def test_twilio_caller_reports_no_answer():
    client = FakeTwilioClient(["ringing", "no-answer"])
    outcome = reminder.TwilioCaller(client, "+15005550006").place_and_wait(
        "+919876543210", "hello"
    )
    assert outcome.result == "no-answer" and outcome.answered is False


def test_twilio_caller_gives_up_watching_but_still_counts_as_sent(monkeypatch):
    monkeypatch.setattr(reminder, "CALL_POLL_SECONDS", 0)
    client = FakeTwilioClient(["ringing"])
    outcome = reminder.TwilioCaller(client, "+15005550006").place_and_wait(
        "+919876543210", "hello"
    )
    assert outcome.result == "unknown-timeout" and outcome.answered is True


def test_twilio_caller_sends_inline_twiml_not_a_url():
    client = FakeTwilioClient(["completed"])
    reminder.TwilioCaller(client, "+15005550006").place_and_wait("+919876543210", "hi there")
    created = client.created[0]
    assert created["to"] == "+919876543210"
    assert created["twiml"].startswith("<Response>")
    assert "hi there" in created["twiml"]


def test_unverified_number_error_gets_a_human_explanation():
    err = RuntimeError("The number is not verified")
    err.code = 21219
    assert "not verified" in reminder.explain_twilio_error(err)
    assert "TEST_OVERRIDE_NUMBER" in reminder.explain_twilio_error(err)


# --------------------------------------------------------------------------
# TwiML Bin URL path (used when the account cannot send inline TwiML)
# --------------------------------------------------------------------------


def test_build_call_url_appends_the_message_as_a_query_param():
    url = reminder.build_call_url("https://handler.twilio.com/twiml/EHxxxx", "Hello Ramesh")
    assert url == "https://handler.twilio.com/twiml/EHxxxx?msg=Hello+Ramesh"


def test_build_call_url_respects_an_existing_query_string():
    url = reminder.build_call_url("https://example.com/t?a=1", "hi")
    assert url == "https://example.com/t?a=1&msg=hi"


def test_build_call_url_escapes_unsafe_characters():
    url = reminder.build_call_url("https://example.com/t", "Pickup at A&B, 9:00 AM")
    assert "%26" in url and " " not in url


def test_caller_uses_the_bin_url_when_twiml_url_is_set(monkeypatch):
    monkeypatch.setattr(reminder, "TWIML_URL", "https://handler.twilio.com/twiml/EHxxxx")
    client = FakeTwilioClient(["completed"])
    reminder.TwilioCaller(client, "+15005550006").place_and_wait("+919876543210", "hi there")
    created = client.created[0]
    assert "twiml" not in created
    assert created["url"].startswith("https://handler.twilio.com/twiml/EHxxxx?msg=")
    # `method` must never be sent: Twilio trial accounts reject it outright.
    assert "method" not in created


def test_caller_uses_inline_twiml_when_twiml_url_is_blank(monkeypatch):
    monkeypatch.setattr(reminder, "TWIML_URL", "")
    client = FakeTwilioClient(["completed"])
    reminder.TwilioCaller(client, "+15005550006").place_and_wait("+919876543210", "hi there")
    assert client.created[0]["twiml"].startswith("<Response>")


def test_trial_inline_twiml_rejection_gets_a_human_explanation():
    err = RuntimeError("Invalid or disallowed parameters provided - trial accounts "
                       "have limited parameter access")
    assert "TWIML_URL" in reminder.explain_twilio_error(err)
