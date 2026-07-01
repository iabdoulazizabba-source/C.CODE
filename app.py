"""A small time & attendance web app.

Attendance is sourced from a physical ZKTeco terminal on the LAN: the
server polls the device, imports its punches, and derives worked hours.

Features:
  * Login / logout with admin and employee roles
  * Employee management + mapping each employee to a device enrollment id
  * Device sync (manual "Sync now" + automatic background polling)
  * Timesheet reports derived from device punches
"""

import csv
import io
import os
import threading
import time
from datetime import date, datetime
from functools import wraps

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)

from device import DeviceError, connector_from_config
from models import (
    AttendancePunch,
    LunchWorked,
    OffshoreMission,
    User,
    add_manual_punch,
    db,
    import_device_users,
    sync_from_device,
    utcnow,
)
from reporting import build_report, build_today
from schedule import DEFAULT_SCHEDULE, round_hours

login_manager = LoginManager()
login_manager.login_view = "login"

# Simple in-memory record of the most recent sync (per process).
_last_sync = {"time": None, "result": None}
_poller_started = False


def create_app(database_uri=None, connector=None, extra_config=None):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        database_uri or "sqlite:///attendance.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Device configuration (overridable via environment variables).
    app.config["DEVICE_DRIVER"] = os.environ.get("DEVICE_DRIVER", "zk")
    app.config["DEVICE_HOST"] = os.environ.get("DEVICE_HOST")
    app.config["DEVICE_PORT"] = int(os.environ.get("DEVICE_PORT", "4370"))
    app.config["DEVICE_PASSWORD"] = int(os.environ.get("DEVICE_PASSWORD", "0"))
    app.config["DEVICE_FORCE_UDP"] = (
        os.environ.get("DEVICE_FORCE_UDP", "").lower() in ("1", "true", "yes")
    )
    app.config["DEVICE_POLL_INTERVAL"] = int(
        os.environ.get("DEVICE_POLL_INTERVAL", "60")
    )
    if extra_config:
        app.config.update(extra_config)

    db.init_app(app)
    login_manager.init_app(app)
    app.add_template_filter(round_hours, "hours")

    # The device connector can be injected (tests) or built from config.
    if connector is not None:
        app.device_connector = connector
    else:
        try:
            app.device_connector = connector_from_config(app.config)
        except Exception:
            app.device_connector = None  # not configured yet; UI will say so

    with app.app_context():
        db.create_all()
        _ensure_schema()
        _seed_admin()

    register_routes(app)
    return app


def _ensure_schema():
    """Add columns introduced after a DB was first created (SQLite-friendly).

    create_all() makes new tables but never alters existing ones, so older
    databases miss newer columns. We add them in place rather than forcing a
    rebuild, keeping any data already synced.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    changed = False

    punch_cols = {c["name"] for c in inspector.get_columns("attendance_punch")}
    punch_additions = {
        "source": "ALTER TABLE attendance_punch "
                  "ADD COLUMN source VARCHAR(10) NOT NULL DEFAULT 'device'",
        "ignored": "ALTER TABLE attendance_punch "
                   "ADD COLUMN ignored BOOLEAN NOT NULL DEFAULT 0",
    }
    for column, ddl in punch_additions.items():
        if column not in punch_cols:
            db.session.execute(text(ddl))
            changed = True

    user_cols = {c["name"] for c in inspector.get_columns("user")}
    if "active" not in user_cols:
        db.session.execute(
            text('ALTER TABLE "user" ADD COLUMN active BOOLEAN NOT NULL DEFAULT 1')
        )
        changed = True
    if "position" not in user_cols:
        db.session.execute(
            text('ALTER TABLE "user" ADD COLUMN position VARCHAR(80)')
        )
        changed = True

    if changed:
        db.session.commit()


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _seed_admin():
    """Create a default admin on first run so you can log in."""
    if User.query.filter_by(role="admin").first() is None:
        # Management login, not a clocking employee -> inactive for attendance.
        admin = User(username="admin", full_name="Administrator", role="admin",
                     active=False)
        admin.set_password(os.environ.get("ADMIN_PASSWORD", "admin123"))
        db.session.add(admin)
        db.session.commit()


def _record_sync(result):
    _last_sync["time"] = utcnow()
    _last_sync["result"] = result


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def register_routes(app):
    @app.context_processor
    def inject_brand():
        # Prefer real uploaded artwork; fall back to the bundled emblem.
        def has(name):
            return os.path.exists(os.path.join(app.static_folder, name))

        return {
            "brand_logo": "logo.png" if has("logo.png") else "logo.svg",
            "has_wordmark": has("wordmark.png"),
            "has_hero": has("hero.jpg"),
            "company_name": os.environ.get(
                "COMPANY_NAME", "PESCHAUD MARITIME CAMEROUN"
            ),
            "fleet": os.environ.get(
                "FLEET", "DAMEN Fast Crew Supplier · CB19 — Via Maris Shipyard"
            ),
        }

    @app.route("/")
    @login_required
    def dashboard():
        sessions = current_user.sessions()
        return render_template(
            "dashboard.html",
            sessions=sessions[:10],
            is_clocked_in=current_user.is_clocked_in,
            last_punch_at=current_user.last_punch_at,
            enrolled=current_user.device_uid is not None,
            last_sync=_last_sync,
        )

    @app.route("/today")
    @admin_required
    def today():
        people = User.query.filter_by(active=True).order_by(User.full_name).all()
        board = build_today(people)
        return render_template("today.html", board=board)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user)
                return redirect(url_for("dashboard"))
            flash("Invalid username or password.", "error")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    @app.route("/sync", methods=["POST"])
    @admin_required
    def sync():
        connector = app.device_connector
        if connector is None:
            flash("No device configured. Set DEVICE_HOST and restart.", "error")
            return redirect(request.referrer or url_for("device"))
        result = sync_from_device(connector)
        _record_sync(result)
        if not result.ok:
            flash(f"Sync failed: {result.error}", "error")
        else:
            msg = f"Sync complete: {result.imported} new punch(es)"
            if result.unmapped:
                msg += f", {result.unmapped} from unmapped device IDs"
            flash(msg + ".", "success")
        return redirect(request.referrer or url_for("device"))

    @app.route("/device/import-users", methods=["POST"])
    @admin_required
    def import_users():
        connector = app.device_connector
        if connector is None:
            flash("No device configured.", "error")
            return redirect(url_for("device"))
        created, skipped, error = import_device_users(connector)
        if error:
            flash(f"Import failed: {error}", "error")
        else:
            flash(
                f"Imported {created} new employee(s) from the device "
                f"({skipped} already linked).",
                "success",
            )
        return redirect(url_for("employees"))

    @app.route("/device")
    @admin_required
    def device():
        connector = app.device_connector
        online = bool(connector and connector.ping())

        device_users = []
        device_error = None
        if connector and online:
            try:
                device_users = connector.fetch_users()
            except DeviceError as exc:
                device_error = str(exc)

        # device_uids that have punches but no mapped employee yet.
        unmapped = (
            db.session.query(
                AttendancePunch.device_uid,
                db.func.count(AttendancePunch.id),
            )
            .filter(AttendancePunch.user_id.is_(None))
            .group_by(AttendancePunch.device_uid)
            .all()
        )

        return render_template(
            "device.html",
            host=app.config.get("DEVICE_HOST"),
            port=app.config.get("DEVICE_PORT"),
            driver=app.config.get("DEVICE_DRIVER"),
            configured=connector is not None,
            online=online,
            device_users=device_users,
            device_error=device_error,
            unmapped=unmapped,
            last_sync=_last_sync,
        )

    @app.route("/employees")
    @admin_required
    def employees():
        people = User.query.order_by(User.full_name).all()
        return render_template("employees.html", people=people)

    @app.route("/employees/add", methods=["POST"])
    @admin_required
    def add_employee():
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "employee")
        device_uid = request.form.get("device_uid", "").strip() or None
        position = request.form.get("position", "").strip() or None

        if not username or not full_name or not password:
            flash("Name, username and password are required.", "error")
        elif User.query.filter_by(username=username).first():
            flash(f"Username '{username}' is already taken.", "error")
        elif device_uid and User.query.filter_by(device_uid=device_uid).first():
            flash(f"Device ID '{device_uid}' is already assigned.", "error")
        else:
            user = User(
                username=username,
                full_name=full_name,
                role=role,
                device_uid=device_uid,
                position=position,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            _link_existing_punches(user)
            flash(f"Added {full_name}.", "success")
        return redirect(url_for("employees"))

    @app.route("/employees/<int:user_id>/device-id", methods=["POST"])
    @admin_required
    def set_device_id(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            abort(404)
        device_uid = request.form.get("device_uid", "").strip() or None
        clash = (
            User.query.filter(
                User.device_uid == device_uid, User.id != user.id
            ).first()
            if device_uid
            else None
        )
        if clash:
            flash(
                f"Device ID '{device_uid}' is already assigned to "
                f"{clash.full_name}.",
                "error",
            )
        else:
            user.device_uid = device_uid
            db.session.commit()
            _link_existing_punches(user)
            flash(f"Updated device ID for {user.full_name}.", "success")
        return redirect(request.referrer or url_for("employees"))

    @app.route("/employees/<int:user_id>/position", methods=["POST"])
    @admin_required
    def set_position(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            abort(404)
        user.position = request.form.get("position", "").strip() or None
        db.session.commit()
        flash(f"Updated position for {user.full_name}.", "success")
        return redirect(request.referrer or url_for("employee_detail", user_id=user.id))

    @app.route("/employees/<int:user_id>/toggle-active", methods=["POST"])
    @admin_required
    def toggle_active(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            abort(404)
        user.active = not user.active
        db.session.commit()
        state = "active" if user.active else "inactive"
        flash(f"{user.full_name} is now {state}.", "success")
        return redirect(request.referrer or url_for("employees"))

    @app.route("/employees/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def delete_employee(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            abort(404)
        if user.id == current_user.id:
            flash("You cannot delete your own account.", "error")
        else:
            db.session.delete(user)
            db.session.commit()
            flash(f"Removed {user.full_name}.", "success")
        return redirect(url_for("employees"))

    @app.route("/employees/<int:user_id>")
    @admin_required
    def employee_detail(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            abort(404)
        punches = sorted(
            user.punches, key=lambda p: p.timestamp, reverse=True
        )[:60]
        return render_template(
            "employee_detail.html",
            person=user,
            sessions=user.sessions()[:30],
            punches=punches,
            missions=user.missions,
        )

    @app.route("/employees/<int:user_id>/punches/add", methods=["POST"])
    @admin_required
    def add_punch(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            abort(404)
        raw = f"{request.form.get('date', '')} {request.form.get('time', '')}".strip()
        try:
            when = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except ValueError:
            flash("Enter a valid date and time.", "error")
            return redirect(url_for("employee_detail", user_id=user.id))
        _, error = add_manual_punch(user, when)
        if error:
            flash(error, "error")
        else:
            flash(f"Added scan at {when.strftime('%Y-%m-%d %H:%M')}.", "success")
        return redirect(url_for("employee_detail", user_id=user.id))

    @app.route("/punches/<int:punch_id>/remove", methods=["POST"])
    @admin_required
    def remove_punch(punch_id):
        punch = db.session.get(AttendancePunch, punch_id)
        if punch is None:
            abort(404)
        user_id = punch.user_id
        if punch.source == "manual":
            db.session.delete(punch)  # admin-entered: delete outright
            flash("Manual scan deleted.", "success")
        else:
            punch.ignored = True  # device scan: soft-ignore so re-sync won't restore
            flash("Device scan ignored (won't count toward hours).", "success")
        db.session.commit()
        return redirect(
            request.referrer or url_for("employee_detail", user_id=user_id)
        )

    @app.route("/employees/<int:user_id>/lunch-worked", methods=["POST"])
    @admin_required
    def toggle_lunch(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            abort(404)
        try:
            day = datetime.strptime(request.form.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date.", "error")
            return redirect(url_for("employee_detail", user_id=user.id))
        existing = LunchWorked.query.filter_by(
            user_id=user.id, work_date=day
        ).first()
        if existing:
            db.session.delete(existing)
            flash(f"Lunch on {day} no longer counted as worked.", "success")
        else:
            db.session.add(LunchWorked(user_id=user.id, work_date=day))
            flash(f"Lunch on {day} credited as worked (+2h).", "success")
        db.session.commit()
        return redirect(url_for("employee_detail", user_id=user.id))

    @app.route("/punches/<int:punch_id>/restore", methods=["POST"])
    @admin_required
    def restore_punch(punch_id):
        punch = db.session.get(AttendancePunch, punch_id)
        if punch is None:
            abort(404)
        punch.ignored = False
        db.session.commit()
        flash("Scan restored.", "success")
        return redirect(
            request.referrer or url_for("employee_detail", user_id=punch.user_id)
        )

    @app.route("/employees/<int:user_id>/missions/add", methods=["POST"])
    @admin_required
    def add_mission(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            abort(404)
        try:
            start = datetime.strptime(
                request.form.get("start_date", ""), "%Y-%m-%d"
            ).date()
            end = datetime.strptime(
                request.form.get("end_date", ""), "%Y-%m-%d"
            ).date()
        except ValueError:
            flash("Enter valid start and end dates.", "error")
            return redirect(url_for("employee_detail", user_id=user.id))
        if end < start:
            flash("End date can't be before start date.", "error")
        else:
            db.session.add(
                OffshoreMission(
                    user_id=user.id,
                    start_date=start,
                    end_date=end,
                    note=request.form.get("note", "").strip() or None,
                )
            )
            db.session.commit()
            flash(f"Offshore mission added ({(end - start).days + 1} day(s)).",
                  "success")
        return redirect(url_for("employee_detail", user_id=user.id))

    @app.route("/missions/<int:mission_id>/delete", methods=["POST"])
    @admin_required
    def delete_mission(mission_id):
        mission = db.session.get(OffshoreMission, mission_id)
        if mission is None:
            abort(404)
        user_id = mission.user_id
        db.session.delete(mission)
        db.session.commit()
        flash("Offshore mission removed.", "success")
        return redirect(url_for("employee_detail", user_id=user_id))

    @app.route("/reports")
    @login_required
    def reports():
        start, end = _parse_range()
        people, selected = _report_people()
        reports = build_report(people, start, end)
        # Show day-by-day detail only when a single employee is in view.
        detail = reports[0] if len(reports) == 1 else None
        return render_template(
            "reports.html",
            reports=reports,
            detail=detail,
            start=start,
            end=end,
            selected=selected,
            all_employees=(
                User.query.order_by(User.full_name).all()
                if current_user.is_admin else []
            ),
            expected_hours=DEFAULT_SCHEDULE.expected_hours,
        )

    @app.route("/reports.csv")
    @login_required
    def reports_csv():
        start, end = _parse_range()
        people, _ = _report_people()
        reports = build_report(people, start, end)
        kind = request.args.get("kind", "summary")

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        if kind == "days":
            writer.writerow(["Employee", "Date", "Weekday", "Status", "In",
                             "Out", "Net hours", "Overtime", "Late", "Left early",
                             "Lunch worked"])
            for rep in reports:
                for d in rep.days:
                    writer.writerow([
                        rep.name, d.day.isoformat(), d.weekday, d.status,
                        d.clock_in.strftime("%H:%M") if d.clock_in else "",
                        d.clock_out.strftime("%H:%M") if d.clock_out else "",
                        round_hours(d.net_hours), round_hours(d.overtime),
                        "yes" if d.late else "", "yes" if d.early else "",
                        "yes" if d.lunch_worked else "",
                    ])
        else:
            writer.writerow(["Employee", "Net hours", "Overtime", "Present days",
                             "Weekend days", "Incomplete", "Late days",
                             "Absent days", "Offshore days"])
            for rep in reports:
                writer.writerow([
                    rep.name, round_hours(rep.net_hours), round_hours(rep.overtime),
                    rep.present_days, rep.weekend_days, rep.incomplete_days,
                    rep.late_days, rep.absent_days, rep.offshore_days,
                ])

        filename = f"timesheet_{start.isoformat()}_{end.isoformat()}_{kind}.csv"
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/reports.pdf")
    @login_required
    def reports_pdf():
        from pdf_report import build_pdf

        start, end = _parse_range()
        people, _ = _report_people()
        reports = build_report(people, start, end)
        detail = reports[0] if len(reports) == 1 else None

        logo = os.path.join(app.static_folder, "logo.png")
        brand = inject_brand()
        pdf_bytes = build_pdf(
            reports,
            period=f"{start.isoformat()} to {end.isoformat()}",
            company=brand["company_name"],
            fleet=brand["fleet"],
            logo_path=logo if os.path.exists(logo) else None,
            detail=detail,
        )
        filename = f"timesheet_{start.isoformat()}_{end.isoformat()}.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f"inline; filename={filename}"},
        )


def _parse_date(value, default):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return default


def _parse_range():
    """Date range from query params; defaults to the current month-to-date."""
    today = date.today()
    start = _parse_date(request.args.get("start"), today.replace(day=1))
    end = _parse_date(request.args.get("end"), today)
    if end < start:
        start, end = end, start
    return start, end


def _report_people():
    """(employees to report on, selected id). Non-admins see only themselves."""
    if not current_user.is_admin:
        return [current_user], current_user.id
    emp_id = request.args.get("employee", type=int)
    if emp_id:
        person = db.session.get(User, emp_id)
        if person:
            return [person], emp_id
    return (
        User.query.filter_by(active=True).order_by(User.full_name).all(),
        None,
    )


def _link_existing_punches(user):
    """Attach already-imported, unmapped punches to a newly mapped user."""
    if not user.device_uid:
        return
    updated = (
        AttendancePunch.query.filter_by(
            device_uid=user.device_uid, user_id=None
        ).update({"user_id": user.id})
    )
    if updated:
        db.session.commit()


def start_background_poller(app):
    """Poll the device every DEVICE_POLL_INTERVAL seconds in a daemon thread."""
    global _poller_started
    if _poller_started or app.device_connector is None:
        return
    _poller_started = True
    interval = max(10, int(app.config.get("DEVICE_POLL_INTERVAL", 60)))

    def loop():
        while True:
            time.sleep(interval)
            with app.app_context():
                try:
                    _record_sync(sync_from_device(app.device_connector))
                except Exception as exc:  # pragma: no cover - defensive
                    from models import SyncResult

                    _record_sync(SyncResult(error=str(exc)))

    threading.Thread(target=loop, name="device-poller", daemon=True).start()


app = create_app()


if __name__ == "__main__":
    # Single process (no reloader) so the background poller thread is stable.
    start_background_poller(app)
    app.run(debug=True, use_reloader=False)
