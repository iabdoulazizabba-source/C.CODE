"""Database models for the time & attendance app.

Attendance comes from a physical ZKTeco terminal: each scan is a raw
*punch* (a device user id + timestamp). We store punches verbatim in
:class:`AttendancePunch` (the source of truth) and *derive* work
sessions and hours by pairing them chronologically.
"""

from collections import defaultdict
from datetime import date, datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

from schedule import DEFAULT_SCHEDULE

db = SQLAlchemy()

# Scans closer together than this (per person) are treated as one — the F18
# fingerprint reader often records the same person two or three times within
# seconds.
DEFAULT_DEDUP_MINUTES = 2


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
    missions = db.relationship(
        "OffshoreMission",
        backref="user",
        cascade="all, delete-orphan",
        order_by="OffshoreMission.start_date.desc()",
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    def sessions(self):
        """Daily work sessions for this user (most recent first).

        Punches flagged ``ignored`` (corrected away by an admin) are skipped.
        """
        active = [p for p in self.punches if not p.ignored]
        return build_sessions(sorted(active, key=lambda p: p.timestamp))

    @property
    def offshore_days(self):
        """Total days marked as offshore missions for this user."""
        return sum(m.days for m in self.missions)

    def is_offshore_on(self, day):
        return any(m.covers(day) for m in self.missions)

    @property
    def last_punch_at(self):
        if not self.punches:
            return None
        return max(p.timestamp for p in self.punches)

    @property
    def is_clocked_in(self):
        """Best-effort: an open (single-scan) session dated today."""
        sess = self.sessions()
        return bool(sess) and sess[0].is_open and sess[0].clock_in.date() == date.today()


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
    # "device" (synced) or "manual" (added by an admin correction).
    source = db.Column(db.String(10), nullable=False, default="device")
    # Soft-delete: an admin marked this scan as bogus. Kept so a re-sync
    # won't re-import it, but excluded from hours.
    ignored = db.Column(db.Boolean, nullable=False, default=False)


class OffshoreMission(db.Model):
    """A date range an employee spent on an offshore mission.

    Offshore days are counted as *days present* (not hours) and must not be
    treated as absences when away from the terminal.
    """

    __tablename__ = "offshore_mission"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    note = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    @property
    def days(self):
        return (self.end_date - self.start_date).days + 1

    def covers(self, day):
        return self.start_date <= day <= self.end_date


class Session:
    """A derived clock-in / clock-out pair (not stored in the DB).

    ``hours`` is the raw on-site span; ``net_hours`` deducts the scheduled
    lunch break and is the figure used for pay. Flags (late, early leave,
    overtime) come from the active work schedule.
    """

    def __init__(self, clock_in, clock_out=None):
        self.clock_in = clock_in
        self.clock_out = clock_out

    @property
    def is_open(self):
        return self.clock_out is None

    @property
    def date(self):
        return self.clock_in.date()

    @property
    def hours(self):
        if self.clock_out is None:
            return 0.0
        return round((self.clock_out - self.clock_in).total_seconds() / 3600, 2)

    @property
    def net_hours(self):
        if self.clock_out is None:
            return 0.0
        return DEFAULT_SCHEDULE.net_hours(self.clock_in, self.clock_out)

    @property
    def overtime_hours(self):
        return DEFAULT_SCHEDULE.overtime_hours(self.net_hours)

    @property
    def is_late(self):
        return DEFAULT_SCHEDULE.is_late(self.clock_in)

    @property
    def lateness_minutes(self):
        return DEFAULT_SCHEDULE.lateness_minutes(self.clock_in)

    @property
    def is_early_leave(self):
        return (not self.is_open) and DEFAULT_SCHEDULE.is_early_leave(self.clock_out)


def dedupe_times(times, window_minutes=DEFAULT_DEDUP_MINUTES):
    """Collapse scans within ``window_minutes`` of the previously kept one."""
    kept = []
    for t in sorted(times):
        if not kept or (t - kept[-1]).total_seconds() >= window_minutes * 60:
            kept.append(t)
    return kept


def build_sessions(punches, dedup_minutes=DEFAULT_DEDUP_MINUTES):
    """Derive one work session per calendar day (most recent first).

    The F18 doesn't tag scans as in/out (punch=255), so for each day we
    de-duplicate near-identical scans, then take the first scan as
    clock-in and the last as clock-out ("first-in / last-out"). A day
    with a single scan is an open/incomplete session (no clock-out).
    """
    by_day = defaultdict(list)
    for punch in punches:
        by_day[punch.timestamp.date()].append(punch.timestamp)

    sessions = []
    for day in sorted(by_day):
        times = dedupe_times(by_day[day], dedup_minutes)
        if not times:
            continue
        if len(times) == 1:
            sessions.append(Session(times[0], None))  # only one scan that day
        else:
            sessions.append(Session(times[0], times[-1]))
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


def _slug(name, fallback):
    keep = "".join(c for c in (name or "").lower() if c.isalnum())
    return keep or fallback


def import_device_users(connector, default_password="changeme123"):
    """Create employee accounts for users enrolled on the device.

    Skips device ids already linked to an account. Returns ``(created,
    skipped, error)``. New accounts get ``default_password`` (they should
    change it) and have their existing punches linked immediately.
    """
    from device import DeviceError

    try:
        device_users = connector.fetch_users()
    except DeviceError as exc:
        return 0, 0, str(exc)

    taken_uids = {
        u.device_uid for u in User.query.filter(User.device_uid.isnot(None)).all()
    }
    existing_names = {u.username for u in User.query.all()}

    created = skipped = 0
    for du in device_users:
        if du.device_uid in taken_uids:
            skipped += 1
            continue
        username = _slug(du.name, f"user{du.device_uid}")
        if username in existing_names:
            username = f"{username}{du.device_uid}"
        user = User(
            username=username,
            full_name=du.name or f"User {du.device_uid}",
            role="employee",
            device_uid=du.device_uid,
        )
        user.set_password(default_password)
        db.session.add(user)
        db.session.flush()  # assign id so we can link punches
        AttendancePunch.query.filter_by(
            device_uid=du.device_uid, user_id=None
        ).update({"user_id": user.id})
        existing_names.add(username)
        taken_uids.add(du.device_uid)
        created += 1

    db.session.commit()
    return created, skipped, None


def add_manual_punch(user, when):
    """Add an admin-entered scan for ``user`` at datetime ``when``.

    Returns ``(punch, error)``; ``error`` is set if an identical scan
    already exists. Manual punches don't collide with device de-dup because
    they carry their own ``device_uid`` namespace when the user has none.
    """
    from sqlalchemy.exc import IntegrityError

    device_uid = user.device_uid or f"manual-{user.id}"
    punch = AttendancePunch(
        device_uid=device_uid,
        user_id=user.id,
        timestamp=when,
        status=0,
        source="manual",
    )
    db.session.add(punch)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return None, "A scan already exists at that exact time."
    return punch, None
