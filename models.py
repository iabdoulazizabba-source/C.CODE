"""Database models for the time & attendance app.

Attendance comes from a physical ZKTeco terminal: each scan is a raw
*punch* (a device user id + timestamp). We store punches verbatim in
:class:`AttendancePunch` (the source of truth) and *derive* work
sessions and hours by pairing them chronologically.
"""

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

    # Enrollment id of this person on the attendance terminal. Links the
    # device's punches to this account. Null = not enrolled on a device.
    device_uid = db.Column(db.String(64), unique=True, nullable=True)

    punches = db.relationship(
        "AttendancePunch", backref="user", cascade="all, delete-orphan"
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    def sessions(self):
        """Paired work sessions for this user (most recent first)."""
        return build_sessions(
            sorted(self.punches, key=lambda p: p.timestamp)
        )

    @property
    def is_clocked_in(self):
        sess = self.sessions()
        return bool(sess) and sess[0].is_open


class AttendancePunch(db.Model):
    """One raw scan pulled from the terminal.

    Unique on (device_uid, timestamp) so re-polling the device never
    imports the same scan twice.
    """

    __tablename__ = "attendance_punch"
    __table_args__ = (
        db.UniqueConstraint("device_uid", "timestamp", name="uq_punch"),
    )

    id = db.Column(db.Integer, primary_key=True)
    device_uid = db.Column(db.String(64), nullable=False, index=True)
    # Null while no employee is mapped to this device_uid yet.
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    timestamp = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.Integer, nullable=False, default=0)
    imported_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Session:
    """A derived clock-in / clock-out pair (not stored in the DB)."""

    def __init__(self, clock_in, clock_out=None):
        self.clock_in = clock_in
        self.clock_out = clock_out

    @property
    def is_open(self):
        return self.clock_out is None

    @property
    def hours(self):
        if self.clock_out is None:
            return 0.0
        return round((self.clock_out - self.clock_in).total_seconds() / 3600, 2)


def build_sessions(punches):
    """Pair an ordered list of punches into work sessions.

    ZKTeco terminals don't reliably tag punches as in/out, so we pair
    them chronologically: 1st = in, 2nd = out, 3rd = in, ... A trailing
    unpaired punch is an open (still clocked-in) session. Returns
    sessions most-recent first.
    """
    sessions = []
    pending_in = None
    for punch in punches:  # assumed sorted ascending by timestamp
        if pending_in is None:
            pending_in = punch.timestamp
        else:
            sessions.append(Session(pending_in, punch.timestamp))
            pending_in = None
    if pending_in is not None:
        sessions.append(Session(pending_in, None))
    sessions.reverse()
    return sessions


class SyncResult:
    """Outcome of a device poll."""

    def __init__(self, imported=0, skipped=0, unmapped=0, error=None):
        self.imported = imported
        self.skipped = skipped
        self.unmapped = unmapped
        self.error = error

    @property
    def ok(self):
        return self.error is None


def sync_from_device(connector):
    """Poll the terminal and import any new punches.

    De-duplicates against existing rows and maps each punch to an
    employee by ``device_uid``. Unmapped punches are still stored (so
    nothing is lost) and reported so an admin can assign them.
    """
    from device import DeviceError

    try:
        punches = connector.fetch_punches()
    except DeviceError as exc:
        return SyncResult(error=str(exc))

    uid_to_user = {
        u.device_uid: u.id
        for u in User.query.filter(User.device_uid.isnot(None)).all()
    }

    imported = skipped = unmapped = 0
    for p in punches:
        exists = AttendancePunch.query.filter_by(
            device_uid=p.device_uid, timestamp=p.timestamp
        ).first()
        if exists:
            skipped += 1
            continue

        user_id = uid_to_user.get(p.device_uid)
        if user_id is None:
            unmapped += 1
        db.session.add(
            AttendancePunch(
                device_uid=p.device_uid,
                user_id=user_id,
                timestamp=p.timestamp,
                status=p.status,
            )
        )
        imported += 1

    db.session.commit()
    return SyncResult(imported=imported, skipped=skipped, unmapped=unmapped)
