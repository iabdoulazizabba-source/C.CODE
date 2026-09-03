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
- the remaining scans are paired into **work segments** (in/out, in/out, …), so
  a lunch check-out/in becomes an unpaid gap, and the segments are summed.

Scan times are rounded to the nearest minute (≥30s up, otherwise down) before
hours are computed, so totals carry no stray seconds.

An **odd** number of scans (a missed clock-out) makes a day incomplete. A past
incomplete workday is **auto-filled as a standard 08:00–18:00 day** (8 net
hours, flagged "auto"); today's in-progress day is left open. Disable with
`AUTOFILL_INCOMPLETE=0`. Raw punches are stored verbatim, so corrections don't
require re-reading the device.

### Work schedule, hours & overtime

The default schedule is **Mon–Fri, 08:00–18:00, lunch 12:00–14:00** → a full
weekday is **8 regular hours**. Pay (net) hours = regular + overtime, where
**overtime** is:

- hours worked **before 08:00**,
- hours worked **after 18:00**,
- the **12:00–14:00 lunch when it was worked** — i.e. the employee did not check
  out for lunch (an admin can override a day to *worked* or *taken*), and
- a flat **10 hours for any weekend day** (Sat/Sun) the employee is present.

The lunch rule only applies from `LUNCH_SCANNING_FROM` (the date crew began
checking in/out for lunch). Days **before** it always deduct the lunch, since
historical data has no lunch scans.

Reports also flag each weekday: **late** (first scan after 08:00 + grace,
default 10 min) and **left early** (last scan before 18:00 − grace). Weekend
days are labelled separately.

Override the schedule with `WORK_START`, `WORK_END`, `BREAK_START`, `BREAK_END`,
`LATE_GRACE_MINUTES`, `WEEKEND_CREDIT_HOURS`, `LUNCH_SCANNING_FROM`, and
`AUTOFILL_INCOMPLETE` (e.g. `WORK_END=17:00`, `LUNCH_SCANNING_FROM=2026-07-06`,
`AUTOFILL_INCOMPLETE=0`).

## Features

- **Login / authentication** with `admin` and `employee` roles
- **Live "Today" board** — who's on-site, not arrived, checked out, offshore, or
  late right now, with a headcount
- **Employee management** + linking each employee to a device enrollment id;
  mark accounts **active / inactive** (inactive ones drop out of attendance)
- **Import users** — create employee accounts straight from the device roster
- **Employee detail page** with manual **punch corrections** (add a forgotten
  scan; ignore a bad device scan so a re-sync won't restore it)
- **Leave & missions** — mark date ranges as **offshore**, **sick**, or
  **holiday**; each counts as days (not hours), is shown per-kind in reports and
  the Today board, and is never flagged as an absence
- **Device page**: connection status, enrolled users, unmapped IDs, manual sync
- **Schedule-aware reports** — net hours (after lunch break) plus late /
  left-early / overtime flags per session
- **Pay-period reports** — filter by date range and employee, with per-day
  **absence detection** (a weekday with no scans and not offshore),
  **CSV export** (summary or detailed) and a **print-ready PDF** (branded
  header/footer with the logo) for payroll
- **Job function / position** per employee, shown in lists, reports, and PDF
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
| `DEVICE_HOST`          | *(unset)*    | Fixed terminal IP, e.g. `192.168.1.201`            |
| `DEVICE_SERIAL`        | *(unset)*    | Auto-find the terminal by serial on any IP (recommended — immune to IP changes/conflicts) |
| `DEVICE_SUBNET`        | from host    | Subnet to scan for `DEVICE_SERIAL`, e.g. `192.168.10` |
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
