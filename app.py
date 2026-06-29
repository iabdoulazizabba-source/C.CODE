"""A small time & attendance web app.

Features:
  * Login / logout with admin and employee roles
  * Employee management (admins only)
  * Clock in / clock out
  * Timesheet reports
"""

import os
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

from models import TimeEntry, User, db, utcnow

login_manager = LoginManager()
login_manager.login_view = "login"


def create_app(database_uri=None):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        database_uri or "sqlite:///attendance.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    login_manager.init_app(app)

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


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def register_routes(app):
    @app.route("/")
    @login_required
    def dashboard():
        open_entry = current_user.open_entry
        recent = (
            TimeEntry.query.filter_by(user_id=current_user.id)
            .order_by(TimeEntry.clock_in.desc())
            .limit(10)
            .all()
        )
        return render_template(
            "dashboard.html", open_entry=open_entry, recent=recent
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

    @app.route("/clock-in", methods=["POST"])
    @login_required
    def clock_in():
        if current_user.open_entry:
            flash("You are already clocked in.", "error")
        else:
            db.session.add(TimeEntry(user_id=current_user.id))
            db.session.commit()
            flash("Clocked in.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/clock-out", methods=["POST"])
    @login_required
    def clock_out():
        entry = current_user.open_entry
        if entry is None:
            flash("You are not clocked in.", "error")
        else:
            entry.clock_out = utcnow()
            db.session.commit()
            flash(f"Clocked out. {entry.hours} hours recorded.", "success")
        return redirect(url_for("dashboard"))

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

        if not username or not full_name or not password:
            flash("All fields are required.", "error")
        elif User.query.filter_by(username=username).first():
            flash(f"Username '{username}' is already taken.", "error")
        else:
            user = User(username=username, full_name=full_name, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"Added {full_name}.", "success")
        return redirect(url_for("employees"))

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
        query = TimeEntry.query.filter(TimeEntry.clock_out.isnot(None))
        if not current_user.is_admin:
            query = query.filter_by(user_id=current_user.id)
        entries = query.order_by(TimeEntry.clock_in.desc()).all()

        totals = defaultdict(float)
        for entry in entries:
            totals[entry.user.full_name] += entry.hours
        totals = dict(sorted(totals.items()))

        return render_template(
            "reports.html", entries=entries, totals=totals
        )


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
