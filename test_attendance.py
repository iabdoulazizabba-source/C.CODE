"""Tests for the time & attendance app (device-sourced)."""

from datetime import datetime

import pytest

from app import create_app
from device import FakeConnector, Punch, DeviceUser
from models import AttendancePunch, User, build_sessions, db, sync_from_device


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


def test_build_sessions_pairs_punches():
    punches = [
        Punch("1", datetime(2026, 1, 1, 9, 0)),
        Punch("1", datetime(2026, 1, 1, 12, 30)),
        Punch("1", datetime(2026, 1, 1, 13, 0)),
    ]
    sessions = build_sessions(punches)  # most recent first
    assert len(sessions) == 2
    assert sessions[0].is_open          # trailing 13:00 punch = open
    assert sessions[1].hours == 3.5     # 09:00 -> 12:30


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


def test_open_session_means_clocked_in(app):
    with app.app_context():
        add_user("carol", device_uid="3003")
        conn = FakeConnector(punches=[Punch("3003", datetime(2026, 1, 1, 8, 0))])
        sync_from_device(conn)
        carol = User.query.filter_by(username="carol").first()
        assert carol.is_clocked_in


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
