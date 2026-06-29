"""Database models for the time & attendance app."""

from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def utcnow():
    """Naive UTC timestamp (consistent with how SQLite stores datetimes)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(UserMixin, db.Model):
    """An employee or admin who can log in."""

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="employee")

    entries = db.relationship(
        "TimeEntry", backref="user", cascade="all, delete-orphan"
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def open_entry(self):
        """The current clock-in with no clock-out yet, if any."""
        return TimeEntry.query.filter_by(
            user_id=self.id, clock_out=None
        ).first()


class TimeEntry(db.Model):
    """A single clock-in / clock-out record."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False
    )
    clock_in = db.Column(db.DateTime, nullable=False, default=utcnow)
    clock_out = db.Column(db.DateTime, nullable=True)

    @property
    def hours(self):
        """Hours worked for this entry (0 while still clocked in)."""
        if self.clock_out is None:
            return 0.0
        delta = self.clock_out - self.clock_in
        return round(delta.total_seconds() / 3600, 2)

    @property
    def is_open(self):
        return self.clock_out is None
