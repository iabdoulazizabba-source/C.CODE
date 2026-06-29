# Time & Attendance

A small web app for tracking employee work hours, built with Flask.

## Features

- **Login / authentication** with two roles: `admin` and `employee`
- **Clock in / clock out** — employees record their work time
- **Employee management** — admins add, list, and remove employees
- **Timesheet reports** — total hours per person plus a full entry log

## Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
python app.py
```

Then open <http://localhost:5000> and log in with the default admin account:

| Username | Password   |
| -------- | ---------- |
| `admin`  | `admin123` |

> **Note:** Change the default password before deploying anywhere real. You can
> override the seeded credentials and secret key with the `ADMIN_PASSWORD` and
> `SECRET_KEY` environment variables.

## Running the tests

```bash
pip install pytest
pytest
```

## Project layout

```
app.py                 # Flask app: routes, auth, app factory
models.py              # SQLAlchemy models (User, TimeEntry)
templates/             # Jinja2 HTML templates
static/style.css       # Styling
test_attendance.py     # Tests
```

The database is a local SQLite file (`attendance.db`) created automatically on
first run.
