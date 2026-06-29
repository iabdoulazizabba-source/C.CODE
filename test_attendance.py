"""Tests for the time & attendance app."""

from datetime import datetime, timedelta

import pytest

from app import create_app
from models import TimeEntry, User, db


@pytest.fixture
def app():
    app = create_app(database_uri="sqlite:///:memory:")
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username="admin", password="admin123"):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def test_password_hashing(app):
    with app.app_context():
        user = User(username="bob", full_name="Bob", role="employee")
        user.set_password("s3cret")
        assert user.password_hash != "s3cret"
        assert user.check_password("s3cret")
        assert not user.check_password("wrong")


def test_entry_hours(app):
    with app.app_context():
        start = datetime(2026, 1, 1, 9, 0, 0)
        entry = TimeEntry(user_id=1, clock_in=start)
        assert entry.hours == 0.0  # still open
        entry.clock_out = start + timedelta(hours=2, minutes=30)
        assert entry.hours == 2.5
        assert not entry.is_open


def test_seed_admin_exists(app):
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        assert admin is not None
        assert admin.is_admin


def test_login_required_redirects(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_and_clock_in_out(client, app):
    login(client)

    resp = client.post("/clock-in", follow_redirects=True)
    assert b"Clocked in" in resp.data

    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        assert admin.open_entry is not None

    resp = client.post("/clock-out", follow_redirects=True)
    assert b"Clocked out" in resp.data

    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        assert admin.open_entry is None


def test_employee_cannot_access_employees_page(client, app):
    with app.app_context():
        worker = User(username="worker", full_name="Worker", role="employee")
        worker.set_password("pw")
        db.session.add(worker)
        db.session.commit()

    login(client, username="worker", password="pw")
    resp = client.get("/employees")
    assert resp.status_code == 403
