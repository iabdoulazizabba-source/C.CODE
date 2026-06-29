"""A small time & attendance web app.

Attendance is sourced from a physical ZKTeco terminal on the LAN: the
server polls the device, imports its punches, and derives worked hours.

Features:
  * Login / logout with admin and employee roles
  * Employee management + mapping each employee to a device enrollment id
  * Device sync (manual "Sync now" + automatic background polling)
  * Timesheet reports derived from device punches
"""

import os
import threading
import time
from collections import defaultdict
from functools import wraps

from flask import (
    Flask,
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
    User,
    db,
    import_device_users,
    sync_from_device,
    utcnow,
)

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
        _seed_admin()

    register_routes(app)
    return app


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _seed_admin():
    """Create a default admin on first run so you can log in."""
    if User.query.filter_by(role="admin").first() is None:
        admin = User(username="admin", full_name="Administrator", role="admin")
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

    @app.route("/reports")
    @login_required
    def reports():
        # Employees see only their own hours; admins see everyone.
        people = (
            User.query.order_by(User.full_name).all()
            if current_user.is_admin
            else [current_user]
        )

        rows = []
        totals = defaultdict(float)
        for person in people:
            for session in person.sessions():
                if session.is_open:
                    continue
                rows.append((person.full_name, session))
                totals[person.full_name] += session.hours

        rows.sort(key=lambda r: r[1].clock_in, reverse=True)
        totals = dict(sorted(totals.items()))
        return render_template("reports.html", rows=rows, totals=totals)


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
