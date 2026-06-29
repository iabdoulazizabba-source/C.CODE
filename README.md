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
4. **Derives** work sessions by pairing punches chronologically
   (1st = in, 2nd = out, …) and totals the hours.

## Features

- **Login / authentication** with `admin` and `employee` roles
- **Employee management** + linking each employee to a device enrollment id
- **Device page**: connection status, enrolled users, unmapped IDs, manual sync
- **Timesheet reports** derived from device punches

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

- Punches are paired chronologically; the device's own in/out tag isn't relied
  on (ZKTeco terminals report it inconsistently). The raw punches are stored, so
  the pairing rule can be refined later without data loss.
- This uses Flask's development server. Put a production WSGI server (e.g.
  `waitress` or `gunicorn`) in front before real deployment.
