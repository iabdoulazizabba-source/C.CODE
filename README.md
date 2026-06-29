# Time & Attendance

A small web app that tracks employee work hours from a physical **ZKTeco**
attendance terminal on your local network. The server polls the device, imports
its punches, and derives worked hours. Built with Flask.

## How it works

```
ZKTeco terminal  ──(Ethernet/LAN, TCP 4370)──►  Flask server  ──►  SQLite
   (punches)            pyzk polling              (this app)       (records)
```

The terminal stores a *punch* (a device enrollment id + timestamp) every time
someone scans. The app:

1. **Polls** the device (manual **Sync now** button + an automatic background
   poller every `DEVICE_POLL_INTERVAL` seconds).
2. **Imports** new punches, de-duplicated so re-polling never double-counts.
3. **Maps** each punch to an employee by their **Device ID** (enrollment id).
   Admins can bulk-create accounts from the device via **Import users**.
4. **Derives** daily work sessions and totals the hours (see below).

### How hours are calculated

The terminal (e.g. a ZKTeco **F18**) records a *punch* per scan but does **not**
tag it as in vs out (`punch=255`). So per employee, per calendar day:

- near-identical scans within ~2 minutes are collapsed (fingerprint readers
  often double- or triple-read the same person), then
- the **first** remaining scan is the clock-in and the **last** is the clock-out
  ("first-in / last-out"), then
- the scheduled **lunch break** (12:00–14:00) is deducted to give **net hours**.

A day with only a single scan is shown as an **open / incomplete** session (no
clock-out) rather than counted as zero hours. Raw punches are stored verbatim,
so this rule can be refined later without re-reading the device.

### Work schedule & flags

The default schedule is **Mon–Fri, 08:00–18:00, lunch 12:00–14:00** → a full day
is **8 net hours**. Reports flag each session:

- **late** — first scan later than 08:00 + grace (default 10 min)
- **left early** — last scan before 18:00 − grace
- **overtime** — net hours beyond the 8h day

Override the schedule with `WORK_START`, `WORK_END`, `BREAK_START`, `BREAK_END`,
and `LATE_GRACE_MINUTES` (e.g. `WORK_END=17:00`).

## Features

- **Login / authentication** with `admin` and `employee` roles
- **Employee management** + linking each employee to a device enrollment id
- **Import users** — create employee accounts straight from the device roster
- **Employee detail page** with manual **punch corrections** (add a forgotten
  scan; ignore a bad device scan so a re-sync won't restore it)
- **Offshore missions** — mark date ranges an employee is offshore; counted as
  days present (not hours) and never flagged as absences
- **Device page**: connection status, enrolled users, unmapped IDs, manual sync
- **Schedule-aware reports** — net hours (after lunch break) plus late /
  left-early / overtime flags per session
- **Pay-period reports** — filter by date range and employee, with per-day
  **absence detection** (a weekday with no scans and not offshore) and
  **CSV export** (summary or detailed) for payroll
- **Timesheet reports** derived from device punches (first-in / last-out, deduped)

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on *nix)
pip install -r requirements.txt

# Point the app at your terminal's IP, then run it:
set DEVICE_HOST=192.168.1.201    # Windows  (export DEVICE_HOST=... on *nix)
python app.py
```

Open <http://localhost:5000> and log in with the default admin:

| Username | Password   |
| -------- | ---------- |
| `admin`  | `admin123` |

Then open **Device → Sync now** to pull punches, and on **Employees** set each
person's **Device ID** to match their enrollment number on the terminal.

### Configuration (environment variables)

| Variable               | Default      | Purpose                                            |
| ---------------------- | ------------ | -------------------------------------------------- |
| `DEVICE_DRIVER`        | `zk`         | `zk` for a real device, `fake` for a demo/no HW    |
| `DEVICE_HOST`          | *(unset)*    | Terminal IP address, e.g. `192.168.1.201`          |
| `DEVICE_PORT`          | `4370`       | ZKTeco TCP port                                    |
| `DEVICE_PASSWORD`      | `0`          | Device comm key, if you set one                    |
| `DEVICE_POLL_INTERVAL` | `60`         | Seconds between automatic syncs                    |
| `ADMIN_PASSWORD`       | `admin123`   | Seeded admin password (change it!)                 |
| `SECRET_KEY`           | `dev-secret` | Flask session secret (set in production)           |

> **Try it without hardware:** run with `DEVICE_DRIVER=fake` to explore the UI
> using an in-memory stand-in device.

## Running the tests

```bash
pip install pytest
pytest
```

The tests use a built-in fake device, so no hardware is required.

## Project layout

```
app.py                 # Flask app: routes, auth, app factory, background poller
device.py              # Device connectors: ZKConnector (pyzk) + FakeConnector
models.py              # Models + punch de-dup + session pairing + sync logic
templates/             # Jinja2 HTML templates
static/style.css       # Styling
test_attendance.py     # Tests (use the fake device)
```

The database is a local SQLite file created automatically on first run.

## Notes & limitations

- Hours use first-in / last-out per day (see above), which counts mid-day breaks
  as worked time unless staff clock out for them. Raw punches are stored, so the
  rule can be refined later without re-reading the device.
- The device's own in/out tag isn't relied on — the tested F18 reports `255`
  (untagged) for every scan.
- This uses Flask's development server. Put a production WSGI server (e.g.
  `waitress` or `gunicorn`) in front before real deployment.

## Verified hardware

Tested live against a **ZKTeco F18** (firmware reporting `punch=255`) over TCP
on the LAN: user import, a 825-record punch sync, de-duplication, and report
generation all confirmed working.
