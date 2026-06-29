"""Tests for the time & attendance app (device-sourced)."""

from datetime import date, datetime, time as dtime

import pytest

from app import create_app
from device import FakeConnector, Punch, DeviceUser
from models import (
    AttendancePunch,
    User,
    build_sessions,
    db,
    import_device_users,
    sync_from_device,
)


def make_app(connector=None):
    return create_app(
        database_uri="sqlite:///:memory:",
        connector=connector or FakeConnector(),
        extra_config={"TESTING": True},
    )


@pytest.fixture
def app():
    yield make_app()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username="admin", password="admin123"):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def add_user(username, device_uid=None, role="employee", password="pw"):
    user = User(
        username=username, full_name=username.title(), role=role,
        device_uid=device_uid,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


# --- model / pairing logic -------------------------------------------------

def test_password_hashing(app):
    with app.app_context():
        user = User(username="bob", full_name="Bob", role="employee")
        user.set_password("s3cret")
        assert user.password_hash != "s3cret"
        assert user.check_password("s3cret")
        assert not user.check_password("wrong")


def test_build_sessions_first_in_last_out_per_day():
    # One day, three scans -> one session spanning first..last (Rule A).
    punches = [
        Punch("1", datetime(2026, 1, 1, 8, 0)),
        Punch("1", datetime(2026, 1, 1, 12, 30)),
        Punch("1", datetime(2026, 1, 1, 17, 0)),
    ]
    sessions = build_sessions(punches)
    assert len(sessions) == 1
    assert sessions[0].hours == 9.0
    assert not sessions[0].is_open


def test_build_sessions_dedupes_near_identical_scans():
    # Duplicate fingerprint reads within ~2 min collapse to one scan.
    punches = [
        Punch("1", datetime(2026, 1, 1, 7, 52, 0)),
        Punch("1", datetime(2026, 1, 1, 7, 52, 30)),  # duplicate -> dropped
        Punch("1", datetime(2026, 1, 1, 17, 53, 0)),
        Punch("1", datetime(2026, 1, 1, 17, 53, 20)),  # duplicate -> dropped
    ]
    sessions = build_sessions(punches)
    assert len(sessions) == 1
    assert sessions[0].hours == round((17 + 53 / 60) - (7 + 52 / 60), 2)


def test_build_sessions_splits_by_day():
    punches = [
        Punch("1", datetime(2026, 1, 1, 8, 0)),
        Punch("1", datetime(2026, 1, 1, 17, 0)),
        Punch("1", datetime(2026, 1, 2, 9, 0)),  # single scan next day = open
    ]
    sessions = build_sessions(punches)  # most recent first
    assert len(sessions) == 2
    assert sessions[0].is_open          # 2026-01-02, one scan
    assert sessions[1].hours == 9.0     # 2026-01-01


def test_seed_admin_exists(app):
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        assert admin is not None and admin.is_admin


# --- device sync -----------------------------------------------------------

def test_sync_imports_maps_and_dedups(app):
    with app.app_context():
        add_user("alice", device_uid="1001")
        conn = FakeConnector(punches=[
            Punch("1001", datetime(2026, 1, 1, 9, 0)),
            Punch("1001", datetime(2026, 1, 1, 17, 0)),
            Punch("2002", datetime(2026, 1, 1, 9, 5)),  # no employee mapped
        ])

        first = sync_from_device(conn)
        assert first.imported == 3
        assert first.unmapped == 1

        # Re-polling the same device must not double-import.
        second = sync_from_device(conn)
        assert second.imported == 0
        assert second.skipped == 3

        alice = User.query.filter_by(username="alice").first()
        sessions = alice.sessions()
        assert len(sessions) == 1
        assert sessions[0].hours == 8.0
        assert not alice.is_clocked_in


def test_single_scan_today_means_clocked_in(app):
    with app.app_context():
        add_user("carol", device_uid="3003")
        today_8am = datetime.combine(date.today(), dtime(8, 0))
        conn = FakeConnector(punches=[Punch("3003", today_8am)])
        sync_from_device(conn)
        carol = User.query.filter_by(username="carol").first()
        assert carol.is_clocked_in  # one scan today, no clock-out yet


def test_import_device_users_creates_linked_accounts(app):
    with app.app_context():
        # A punch arrives before any account exists for this device id.
        sync_from_device(FakeConnector(
            punches=[Punch("77", datetime(2026, 1, 1, 8, 0)),
                     Punch("77", datetime(2026, 1, 1, 17, 0))],
        ))
        conn = FakeConnector(users=[DeviceUser("77", "MEKINDA")])

        created, skipped, error = import_device_users(conn)
        assert error is None
        assert created == 1

        u = User.query.filter_by(device_uid="77").first()
        assert u is not None and u.full_name == "MEKINDA"
        # existing punches got linked, so hours are computed
        assert u.sessions()[0].hours == 9.0

        # Running again skips the already-linked device user.
        created2, skipped2, _ = import_device_users(conn)
        assert created2 == 0 and skipped2 == 1


def test_mapping_after_import_links_existing_punches(app, client):
    with app.app_context():
        conn = FakeConnector(punches=[
            Punch("9009", datetime(2026, 1, 1, 9, 0)),
            Punch("9009", datetime(2026, 1, 1, 17, 0)),
        ])
        sync_from_device(conn)
        # punches imported but unmapped
        assert AttendancePunch.query.filter_by(user_id=None).count() == 2

    login(client)
    with app.app_context():
        dave = add_user("dave")  # no device id yet
        resp = client.post(
            f"/employees/{dave.id}/device-id",
            data={"device_uid": "9009"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        dave = User.query.filter_by(username="dave").first()
        assert AttendancePunch.query.filter_by(user_id=dave.id).count() == 2
        assert dave.sessions()[0].hours == 8.0


def test_sync_reports_error_when_device_unreachable(app):
    with app.app_context():
        result = sync_from_device(FakeConnector(reachable=False))
        assert not result.ok
        assert "unreachable" in result.error.lower()


# --- auth / access control -------------------------------------------------

def test_login_required_redirects(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_employee_cannot_access_admin_pages(client, app):
    with app.app_context():
        add_user("worker", role="employee")
    login(client, username="worker", password="pw")
    assert client.get("/employees").status_code == 403
    assert client.get("/device").status_code == 403


def test_admin_can_view_device_page(client, app):
    conn = FakeConnector(
        users=[DeviceUser("1001", "Alice")],
        punches=[Punch("1001", datetime(2026, 1, 1, 9, 0))],
    )
    app2 = make_app(connector=conn)
    c = app2.test_client()
    login(c)
    resp = c.get("/device")
    assert resp.status_code == 200
    assert b"Alice" in resp.data
