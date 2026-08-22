# Driver Pickup Reminder Agent (v1)

Calls each driver 30 minutes before their scheduled pickup and plays a voice
reminder: call the customer to confirm, and head to the pickup location on
time. Reads and writes a Google Sheet. No manual follow-up from the fleet
manager.

---

## How it works

The agent is a single script that runs **once a minute** from cron (or Windows
Task Scheduler). Every run is a complete, independent sweep — nothing is
remembered between runs, because **all state lives in the sheet**.

One run does this:

```
read all rows
  └─ for each row with a blank "Reminder Status":
       parse pickup time  → pin to Asia/Kolkata
       normalize phone    → E.164 for Twilio
       ├─ bad time or phone?          → SKIPPED_BAD_DATA, next row
       ├─ pickup already passed?      → SKIPPED_TOO_LATE,  next row
       ├─ more than 30 min away?      → leave blank, a later tick handles it
       └─ within the next 30 min?     → DUE:
            1. write "CALLING" to the sheet      ← claim the row first
            2. place the Twilio call (Twilio-hosted TwiML, text-to-speech)
            3. poll until the call reaches a final state
            4. write SENT / FAILED + call result + SID + timestamp
```

The sheet gains four columns the agent owns:

| Column | Name | Values |
|---|---|---|
| E | Reminder Status | *(blank)* → `CALLING` → `SENT` \| `FAILED` \| `SKIPPED_TOO_LATE` \| `SKIPPED_BAD_DATA` |
| F | Call SID | Twilio's id for the call, for looking it up in the console |
| G | Call Result | `completed`, `no-answer`, `busy`, `failed`, or an error message |
| H | Last Updated | When the agent last touched this row (IST) |

A non-blank status means "this row is handled" — that single rule is what
prevents duplicate calls.

### Three decisions worth explaining

**1. The row is claimed *before* the call is placed.**
The agent writes `CALLING` to the sheet, and only then dials. If the process
dies mid-call — power cut, killed job, network drop — the row stays `CALLING`
forever and is never dialled again.

That is deliberate. The alternative (mark it after the call succeeds) risks
dialling a driver twice if the write fails, and a driver who gets the same
robocall twice stops trusting the system. **A rare missed reminder is cheaper
than a repeat call.** If a row is stuck at `CALLING`, the fleet manager can see
it in the sheet and call that driver themselves — which is exactly the visible,
manual fallback you want for a rare failure.

Only one sweep runs at a time (`reminder.lock`). Waiting for a call to finish
can take up to a minute, so a tick can outlive the cron interval — and two
overlapping ticks would both see a blank status and both dial. Claiming the row
guards sequential re-runs; the lock guards concurrent ones.

It takes a real OS-level file lock rather than just creating a file. My first
attempt did the latter, with a timeout to expire abandoned locks, and it wedged
the agent the first time I interrupted a run mid-call: the file outlived the
process that made it. The kernel releases an OS lock however the process
exits — clean return, exception, Ctrl+C, closed terminal — so an interrupted
run cannot block the next one, and the expiry logic disappears entirely.

**2. Every run is a full sweep, not a schedule.**
The agent does not "wake up at T-30 for ride X". It asks, every minute, "which
rides are due and unhandled?" This means there is **no retry logic anywhere in
the codebase** — if a run fails for any reason, the next run is 60 seconds
away, and that is the retry. It also means the trigger window is a catch-up
window: if the machine was asleep from 08:30 and the first run happens at
08:52, a 09:00 pickup still gets its call at T-8. Late beats never.

**3. Twilio hosts the call script, so this project runs no server.**
Twilio normally fetches call instructions from a URL you host — which usually
means a web server, a public hostname, and a tunnel during development. This
project has none of that.

The first design passed the TwiML inline to `calls.create()`. That works on a
paid account and is the simplest thing possible. **Twilio trial accounts reject
it** (HTTP 400, "trial accounts have limited parameter access"), which I only
found by placing a real call — so the code supports both: set `TWIML_URL` to a
**TwiML Bin** (Twilio-hosted, free, no server of ours) and the spoken text is
passed as the `msg` template variable; leave it blank on an upgraded account
and the TwiML goes inline.

Two things trial accounts also reject, both discovered the same way and both
now handled: the `method` parameter on `calls.create()` (the default POST is
fine, and the query string survives it), and cancelling a call over the API.

The remaining trade-off is that call outcomes are learned by polling for up to
60 seconds rather than pushed to a webhook. At one call per minute, free.

---

## Setup

**1. Install**

```bash
pip install -r requirements.txt
```

**2. Prepare the sheet**

Open your rides sheet and add these four headers in row 1, in columns **E, F,
G, H**, spelled exactly:

```
Reminder Status | Call SID | Call Result | Last Updated
```

The agent refuses to run if they are missing, and tells you so. It never
changes the sheet's structure on its own — the sheet belongs to operations.

`rides_seed.csv` in this repo is the sample sheet with those four columns
already added. Import it (File → Import → Upload → *Replace spreadsheet*) and
name the tab `Rides` to get a working sheet in one step.

**3. Give the agent access to the sheet**

1. Go to <https://console.cloud.google.com>, create (or pick) a project.
2. Enable the **Google Sheets API** and the **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create service account**, then
   **Keys → Add key → JSON**. Save the file as `service_account.json` next to
   `reminder.py`.
4. Copy the service account's email (it ends in `.iam.gserviceaccount.com`)
   and **share the sheet with it as an Editor**, exactly like sharing with a
   colleague. This is the step people forget.

**4. Set up Twilio**

1. Create a free trial account at <https://twilio.com/try-twilio>.
2. Copy the **Account SID** and **Auth Token** from the console dashboard.
3. Get a trial phone number (Phone Numbers → Buy a number, free on trial).
4. **Verify your own mobile number** under Phone Numbers → Verified Caller IDs.
   Trial accounts can only dial verified numbers.
5. **On a trial account, create a TwiML Bin** (Develop → TwiML Bins → Create),
   paste the XML below, save it, and put its URL in `TWIML_URL` in `.env`.
   On an upgraded account you can leave `TWIML_URL` blank instead and the
   TwiML is sent inline.

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <Response>
     <Say voice="alice" language="en-IN">{{msg}}</Say>
     <Pause length="1"/>
     <Say voice="alice" language="en-IN">{{msg}}</Say>
   </Response>
   ```

**5. Configure**

```bash
cp .env.example .env
```

Fill in `SHEET_ID` (the long id in the sheet's URL), the three `TWILIO_*`
values, and `TWIML_URL` if you are on a trial account (step 4). While testing,
set `TEST_OVERRIDE_NUMBER` to your own verified number — every reminder is then
redirected to your phone instead of the drivers' real numbers.

**6. Schedule it**

Linux/macOS:

```bash
* * * * * cd /path/to/driver-reminder && /usr/bin/python3 reminder.py >> cron.log 2>&1
```

Windows Task Scheduler: create a task, trigger *Daily*, repeat every *1 minute*
indefinitely, action `python` with argument `reminder.py`, "Start in" set to
this folder.

---

## Running and testing

```bash
python reminder.py                              # normal run
python reminder.py --dry-run                    # everything except dialling
python reminder.py --now "2026-08-21 08:35"     # pretend it is this time
python -m pytest -q                             # 64 unit tests
```

`--dry-run` still reads and writes the sheet — only the dial is skipped. It is
the safest way to confirm Google access and the sheet layout before spending
any Twilio credit.

`--now` is the fake clock. Combined with `--dry-run` you can replay a whole
day against the sheet in a few seconds without waiting for real pickup times.

### Suggested test sequence

1. `python reminder.py --dry-run --now "2026-08-21 08:35"` — confirm exactly
   the right row is picked up and the sheet flips to `SENT`.
2. **Run the same command five times.** Only the first does anything. This is
   the duplicate-prevention guarantee.
3. Clear the status cell for one row, set `TEST_OVERRIDE_NUMBER`, and run for
   real — your phone rings with the reminder.
4. Decline the call — the sheet records `no-answer` / `FAILED`.
5. Set a row's pickup time to ~31 minutes from now, leave cron running, walk
   away. The call arrives unattended.

The sample rows carry fixed dates, so running without `--now` marks them all
`SKIPPED_TOO_LATE` — which is correct behaviour, not a bug. Use `--now`, or
edit a pickup time to today, when you want a row to be due.

Every run also appends to `reminders.log`.

---

## Limitations, and what I'd do next

**Known limits of v1, by design:**

- A row stuck at `CALLING` after a crash is never retried — visible in the
  sheet, but it needs a human. See decision 1 above; this is the trade I chose.
- The reminder is fire-and-forget. `SENT` means the call connected, not that
  the driver was listening or acted on it.
- Twilio trial accounts only dial verified numbers, so real driver numbers
  cannot be used until the account is upgraded, and they require the TwiML Bin
  described in setup step 4.
- The reminder text travels to the TwiML Bin as a URL query parameter. That is
  fine for a driver name and a pickup location, but on an upgraded account the
  inline-TwiML path (blank `TWIML_URL`) avoids it entirely, and is what I would
  use in production.
- Sheets API quotas make this comfortable into the hundreds of rows, not
  thousands.

**With more time, in the order I'd build them:**

1. **DTMF acknowledgement** — "press 1 to confirm". This is the biggest gap:
   right now we know the phone was answered, not that the message landed.
   Turns the reminder from fire-and-forget into a closed loop.
2. **Escalation** — no answer after two attempts, notify the fleet manager.
   Cheap to add once acknowledgement exists, and it is the actual operational
   goal: someone finds out before the customer does.
3. **Hindi (or driver-preferred language) TTS** via `<Say language="hi-IN">`.
   For an NCR driver pool this probably matters more than anything else on
   this list, and it is a one-line change plus a language column.
4. **SMS fallback** when the call fails — a message the driver can read later
   costs almost nothing and covers the no-answer case.
5. **GPS/ETA-based triggering** instead of a fixed 30 minutes, so a driver who
   is already 40 minutes away gets called earlier. This is the real fix for
   late pickups, and the reason v1 stays clock-based is that it needs a
   location feed that doesn't exist yet.
6. **Batching overlapping rides** for one driver into a single call.
7. **Sheets → Postgres** past a few hundred rides a day, at which point the
   one-minute sweep should become a proper queue.
