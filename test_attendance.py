"""Tests for the time & attendance app (device-sourced)."""

from datetime import date, datetime, time as dtime

import pytest

from app import create_app
from device import FakeConnector, Punch, DeviceUser
from models import Session
from reporting import build_report, build_today
from schedule import DEFAULT_SCHEDULE, Schedule, round_hours
from models import (
    AttendancePunch,
    OffshoreMission,
    User,
    add_manual_punch,
    build_sessions,
    db,
    import_device_users,
    round_to_minute,
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


def add_user(username, device_uid=None, role="employee", password="pw",
             position=None):
    user = User(
        username=username, full_name=username.title(), role=role,
        device_uid=device_uid, position=position,
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


def test_build_sessions_lunch_taken_vs_worked():
    # 4 scans = checked out and back in for lunch -> lunch unpaid -> 8h net.
    taken = build_sessions([
        Punch("1", datetime(2026, 1, 1, 8, 0)),
        Punch("1", datetime(2026, 1, 1, 12, 0)),
        Punch("1", datetime(2026, 1, 1, 14, 0)),
        Punch("1", datetime(2026, 1, 1, 18, 0)),
    ])
    assert len(taken) == 1
    s = taken[0]
    assert not s.is_open
    assert s.regular_hours == 8.0 and s.overtime_hours == 0.0
    assert s.net_hours == 8.0 and not s.worked_break

    # 2 scans = no lunch check -> worked through -> +2h overtime.
    worked = build_sessions([
        Punch("1", datetime(2026, 1, 1, 8, 0)),
        Punch("1", datetime(2026, 1, 1, 18, 0)),
    ])[0]
    assert worked.net_hours == 10.0 and worked.overtime_hours == 2.0
    assert worked.worked_break


def test_build_sessions_odd_scans_leave_day_open():
    # A missed clock-out (odd scan count) leaves the day incomplete.
    s = build_sessions([
        Punch("1", datetime(2026, 1, 1, 8, 0)),
        Punch("1", datetime(2026, 1, 1, 12, 0)),
        Punch("1", datetime(2026, 1, 1, 14, 0)),
    ])[0]
    assert s.is_open


def test_build_sessions_dedupes_near_identical_scans():
    # Duplicate fingerprint reads within ~2 min collapse to one scan.
    s = build_sessions([
        Punch("1", datetime(2026, 1, 1, 7, 52, 0)),
        Punch("1", datetime(2026, 1, 1, 7, 52, 30)),  # duplicate -> dropped
        Punch("1", datetime(2026, 1, 1, 17, 53, 0)),
        Punch("1", datetime(2026, 1, 1, 17, 53, 20)),  # duplicate -> dropped
    ])[0]
    assert len(s.scans) == 2
    assert s.clock_in.strftime("%H:%M") == "07:52"
    assert s.clock_out.strftime("%H:%M") == "17:53"


def test_build_sessions_splits_by_day():
    punches = [
        Punch("1", datetime(2026, 1, 1, 8, 0)),
        Punch("1", datetime(2026, 1, 1, 18, 0)),
        Punch("1", datetime(2026, 1, 2, 9, 0)),  # single scan next day = open
    ]
    sessions = build_sessions(punches)  # most recent first
    assert len(sessions) == 2
    assert sessions[0].is_open           # 2026-01-02, one scan
    assert sessions[1].hours == 10.0     # 2026-01-01, 08:00-18:00 continuous


def test_seed_admin_exists(app):
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        assert admin is not None and admin.is_admin


def test_round_hours_whole():
    assert round_hours(8.25) == 8
    assert round_hours(8.5) == 9
    assert round_hours(146.33) == 146
    assert round_hours(10.0) == 10
    assert round_hours(0.0) == 0


def test_round_to_minute():
    assert round_to_minute(datetime(2026, 1, 1, 8, 0, 49)) == datetime(2026, 1, 1, 8, 1)
    assert round_to_minute(datetime(2026, 1, 1, 8, 0, 29)) == datetime(2026, 1, 1, 8, 0)
    assert round_to_minute(datetime(2026, 1, 1, 8, 0, 30)) == datetime(2026, 1, 1, 8, 1)


def test_sessions_round_to_whole_minutes(app):
    with app.app_context():
        u = add_user("pat", device_uid="40")
        add_manual_punch(u, datetime(2026, 2, 2, 8, 0, 49))   # -> 08:01
        add_manual_punch(u, datetime(2026, 2, 2, 16, 0, 20))  # -> 16:00
        s = User.query.filter_by(username="pat").first().sessions()[0]
        assert s.clock_in.second == 0 and s.clock_out.second == 0
        assert s.clock_in.strftime("%H:%M") == "08:01"
        assert s.clock_out.strftime("%H:%M") == "16:00"


def test_set_account_changes_username_and_password(client, app):
    with app.app_context():
        uid = add_user("bea", device_uid="45", password="old").id
        carl_id = add_user("carl", device_uid="46").id
    login(client)

    client.post(f"/employees/{uid}/account",
                data={"username": "beatrice", "password": "newpass"},
                follow_redirects=True)
    with app.app_context():
        u = db.session.get(User, uid)
        assert u.username == "beatrice" and u.check_password("newpass")

    # Blank password keeps the existing one.
    client.post(f"/employees/{uid}/account",
                data={"username": "beatrice", "password": ""},
                follow_redirects=True)
    with app.app_context():
        assert db.session.get(User, uid).check_password("newpass")

    # Duplicate username is rejected.
    client.post(f"/employees/{carl_id}/account",
                data={"username": "beatrice", "password": ""},
                follow_redirects=True)
    with app.app_context():
        assert db.session.get(User, carl_id).username == "carl"


def test_set_position_route(client, app):
    with app.app_context():
        uid = add_user("ralph", device_uid="42").id
    login(client)
    client.post(f"/employees/{uid}/position",
                data={"position": "Chief Engineer"}, follow_redirects=True)
    with app.app_context():
        assert User.query.filter_by(username="ralph").first().position == "Chief Engineer"


def test_pdf_report_download(client, app):
    with app.app_context():
        u = add_user("quinn", device_uid="41", position="Captain")
        add_manual_punch(u, datetime(2026, 3, 2, 8, 0))
        add_manual_punch(u, datetime(2026, 3, 2, 18, 0))
        uid = u.id
    login(client)
    resp = client.get(f"/reports.pdf?start=2026-03-01&end=2026-03-31&employee={uid}")
    assert resp.status_code == 200
    assert resp.content_type == "application/pdf"
    assert resp.data[:4] == b"%PDF"


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


# --- corrections & offshore missions --------------------------------------

def test_manual_punch_creates_session(app):
    with app.app_context():
        user = add_user("erin", device_uid="50")
        add_manual_punch(user, datetime(2026, 2, 2, 8, 0))
        add_manual_punch(user, datetime(2026, 2, 2, 16, 0))
        erin = User.query.filter_by(username="erin").first()
        assert erin.sessions()[0].hours == 8.0
        assert all(p.source == "manual" for p in erin.punches)


def test_ignored_punch_excluded_from_hours(app):
    with app.app_context():
        user = add_user("frank", device_uid="60")
        sync_from_device(FakeConnector(punches=[
            Punch("60", datetime(2026, 2, 3, 8, 0)),
            Punch("60", datetime(2026, 2, 3, 18, 0)),
        ]))
        frank = User.query.filter_by(username="frank").first()
        assert frank.sessions()[0].hours == 10.0

        # Ignore the late "out" scan -> only one active scan that day -> open.
        late = max(frank.punches, key=lambda p: p.timestamp)
        late.ignored = True
        db.session.commit()
        sess = User.query.filter_by(username="frank").first().sessions()
        assert sess[0].is_open


def test_net_hours_single_span():
    s = Schedule()  # default: 08:00-18:00, break 12:00-14:00
    assert s.expected_hours == 8.0
    # A continuous span counts the lunch as worked (no lunch check).
    assert s.net_hours(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 18, 0)) == 10.0
    assert s.net_hours(datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 13, 0)) == 5.0


def test_schedule_late_and_early_flags():
    s = Schedule()
    assert not s.is_late(datetime(2026, 1, 5, 8, 0))     # exactly on time
    assert not s.is_late(datetime(2026, 1, 5, 8, 10))    # within grace
    assert s.is_late(datetime(2026, 1, 5, 8, 30))
    assert s.lateness_minutes(datetime(2026, 1, 5, 8, 30)) == 20  # past 08:10
    assert s.is_early_leave(datetime(2026, 1, 5, 17, 0))
    assert not s.is_early_leave(datetime(2026, 1, 5, 18, 0))


def test_compute_hours_from_segments():
    s = Schedule()

    def t(h, m=0):
        return datetime(2026, 1, 5, h, m)  # a Monday

    # Lunch taken (two segments) -> 8 regular, no break, no extra.
    b = s.compute_hours([(t(8), t(12)), (t(14), t(18))])
    assert (b.regular, b.break_worked, b.total) == (8.0, 0.0, 8.0)
    # Worked through (one segment) -> lunch counts as overtime.
    b2 = s.compute_hours([(t(8), t(18))])
    assert b2.break_worked == 2.0 and b2.total == 10.0
    # Early + late, continuous.
    b3 = s.compute_hours([(t(6), t(20))])
    assert (b3.early, b3.late, b3.break_worked, b3.total) == (2.0, 2.0, 2.0, 14.0)
    # Override forces lunch taken even on a continuous span.
    b4 = s.compute_hours([(t(8), t(18))], lunch_override=False)
    assert b4.break_worked == 0.0 and b4.total == 8.0
    # Override forces lunch worked even though they checked out.
    b5 = s.compute_hours([(t(8), t(12)), (t(14), t(18))], lunch_override=True)
    assert b5.break_worked == 2.0 and b5.total == 10.0


def test_lunch_override_route(client, app):
    with app.app_context():
        uid = add_user("sam", device_uid="43").id
        sam = db.session.get(User, uid)
        # Checked out and back in for lunch -> 8h by default (Monday).
        for h in (8, 12, 14, 18):
            add_manual_punch(sam, datetime(2026, 3, 2, h, 0))
        assert db.session.get(User, uid).sessions()[0].net_hours == 8.0
    login(client)

    client.post(f"/employees/{uid}/lunch",
                data={"date": "2026-03-02", "state": "worked"}, follow_redirects=True)
    with app.app_context():
        s = db.session.get(User, uid).sessions()[0]
        assert s.net_hours == 10.0 and s.overtime_hours == 2.0

    client.post(f"/employees/{uid}/lunch",
                data={"date": "2026-03-02", "state": "auto"}, follow_redirects=True)
    with app.app_context():
        assert db.session.get(User, uid).sessions()[0].net_hours == 8.0


def test_lunch_indicator_in_day_report(app):
    with app.app_context():
        uid = add_user("tina", device_uid="44").id
        tina = db.session.get(User, uid)
        add_manual_punch(tina, datetime(2026, 3, 2, 8, 0))
        add_manual_punch(tina, datetime(2026, 3, 2, 18, 0))  # no lunch check
        rep = build_report([db.session.get(User, uid)],
                           date(2026, 3, 2), date(2026, 3, 2),
                           today=date(2026, 3, 31))[0]
        assert rep.days[0].lunch_worked and rep.days[0].overtime == 2.0


def test_compute_hours_weekend_flat_credit():
    s = Schedule()
    # Saturday 2026-01-03: present for any span -> flat 10h, all overtime.
    b = s.compute_hours([(datetime(2026, 1, 3, 9, 0), datetime(2026, 1, 3, 13, 0))])
    assert (b.weekend, b.regular, b.extra, b.total) == (10.0, 0.0, 10.0, 10.0)


def test_session_exposes_schedule_flags():
    # Continuous 08:30-19:00 (no lunch check) -> worked through.
    late_long = Session([datetime(2026, 1, 5, 8, 30), datetime(2026, 1, 5, 19, 0)])
    assert late_long.is_late
    assert late_long.net_hours == 10.5     # 7.5 regular + 1h late + 2h lunch
    assert late_long.overtime_hours == 3.0
    on_time = Session([datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 16, 0)])
    assert not on_time.is_late
    assert on_time.is_early_leave          # left before 18:00


def test_offshore_mission_days_and_coverage(app):
    with app.app_context():
        user = add_user("gwen")
        db.session.add(OffshoreMission(
            user_id=user.id,
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 10),
            note="rig",
        ))
        db.session.commit()
        gwen = User.query.filter_by(username="gwen").first()
        assert gwen.offshore_days == 10
        assert gwen.is_offshore_on(date(2026, 3, 5))
        assert not gwen.is_offshore_on(date(2026, 3, 11))


def test_report_flags_present_absent_offshore(app):
    with app.app_context():
        user = add_user("iris", device_uid="80")
        # Worked Mon 2026-03-02 and Tue 2026-03-03, checking out for lunch.
        for day in (2, 3):
            for h in (8, 12, 14, 18):
                add_manual_punch(user, datetime(2026, 3, day, h, 0))
        # Offshore Thu-Fri 2026-03-05..06.
        db.session.add(OffshoreMission(
            user_id=user.id, start_date=date(2026, 3, 5), end_date=date(2026, 3, 6),
        ))
        db.session.commit()
        iris = User.query.filter_by(username="iris").first()

        # Mon 2026-03-02 .. Fri 2026-03-06 (Wed is a workday with no scans).
        rep = build_report([iris], date(2026, 3, 2), date(2026, 3, 6),
                           today=date(2026, 3, 31))[0]
        assert rep.present_days == 2
        assert rep.absent_days == 1          # Wed 03-04
        assert rep.offshore_days == 2        # Thu+Fri
        assert rep.net_hours == 16.0         # 2 full days x 8h
        statuses = {d.day.day: d.status for d in rep.days}
        assert statuses[4] == "absent"
        assert statuses[5] == "offshore"


def test_report_weekend_credits_ten_hours(app):
    with app.app_context():
        user = add_user("owen", device_uid="83")
        # Saturday 2026-03-07: present (two scans).
        add_manual_punch(user, datetime(2026, 3, 7, 9, 0))
        add_manual_punch(user, datetime(2026, 3, 7, 12, 0))
        owen = User.query.filter_by(username="owen").first()
        rep = build_report([owen], date(2026, 3, 7), date(2026, 3, 8),
                           today=date(2026, 3, 31))[0]
        assert rep.weekend_days == 1
        assert rep.net_hours == 10.0     # flat weekend credit
        assert rep.overtime == 10.0
        assert {d.day.day: d.status for d in rep.days}[7] == "weekend"


def test_report_skips_weekends_and_future(app):
    with app.app_context():
        user = add_user("jack", device_uid="81")
        # Sat 2026-03-07 has no scans -> must NOT be absent (weekend).
        rep = build_report([user], date(2026, 3, 7), date(2026, 3, 8),
                           today=date(2026, 3, 31))[0]
        assert rep.absent_days == 0
        assert rep.days == []

        # Future days aren't evaluated.
        rep2 = build_report([user], date(2026, 3, 2), date(2026, 3, 31),
                            today=date(2026, 3, 4))[0]
        assert all(d.day <= date(2026, 3, 4) for d in rep2.days)


def test_reports_csv_download(client, app):
    with app.app_context():
        user = add_user("kate", device_uid="82")
        add_manual_punch(user, datetime(2026, 3, 2, 8, 0))
        add_manual_punch(user, datetime(2026, 3, 2, 18, 0))
        uid = user.id
    login(client)
    resp = client.get(
        f"/reports.csv?kind=summary&start=2026-03-01&end=2026-03-31&employee={uid}"
    )
    assert resp.status_code == 200
    assert "text/csv" in resp.content_type
    assert b"Net hours" in resp.data
    assert b"Kate" in resp.data


def test_today_board_statuses(app):
    with app.app_context():
        monday = date(2026, 3, 2)
        on = add_user("onsite", device_uid="90")
        add_manual_punch(on, datetime(2026, 3, 2, 8, 5))   # one scan -> on site
        out = add_user("doneuser", device_uid="91")
        add_manual_punch(out, datetime(2026, 3, 2, 8, 0))
        add_manual_punch(out, datetime(2026, 3, 2, 17, 0))  # two scans -> checked out
        add_user("missing", device_uid="92")               # no scans -> not arrived
        off = add_user("offsh", device_uid="93")
        db.session.add(OffshoreMission(
            user_id=off.id, start_date=date(2026, 3, 1), end_date=date(2026, 3, 5)))
        db.session.commit()

        users = [User.query.filter_by(username=u).first()
                 for u in ("onsite", "doneuser", "missing", "offsh")]
        board = build_today(users, today=monday)
        status = {r.name: r.status for r in board.rows}
        assert status["Onsite"] == "on_site"
        assert status["Doneuser"] == "checked_out"
        assert status["Missing"] == "not_arrived"
        assert status["Offsh"] == "offshore"
        assert (board.on_site, board.checked_out, board.not_arrived,
                board.offshore) == (1, 1, 1, 1)


def test_today_board_weekend_has_no_absences(app):
    with app.app_context():
        add_user("nora", device_uid="94")
        saturday = date(2026, 3, 7)
        board = build_today([User.query.filter_by(username="nora").first()],
                            today=saturday)
        assert not board.is_workday
        assert board.not_arrived == 0
        assert board.rows == []


def test_toggle_active_excludes_from_reports(client, app):
    with app.app_context():
        user = add_user("liam", device_uid="95")
        uid = user.id
    login(client)
    client.post(f"/employees/{uid}/toggle-active", follow_redirects=True)
    with app.app_context():
        assert not User.query.filter_by(username="liam").first().active

    # Inactive users drop out of the "all" population (only the admin, also
    # inactive, remains), so there's nothing to report.
    resp = client.get("/reports")
    assert resp.status_code == 200
    assert b"No employees to report" in resp.data
    # ...but an admin can still pull up the former employee directly.
    direct = client.get(f"/reports?employee={uid}")
    assert b"Liam" in direct.data


def test_today_route_renders(client, app):
    with app.app_context():
        add_user("mia", device_uid="96")
    login(client)
    resp = client.get("/today")
    assert resp.status_code == 200
    assert b"On site" in resp.data


def test_add_punch_route_and_employee_detail(client, app):
    with app.app_context():
        user = add_user("helen", device_uid="70")
        uid = user.id
    login(client)

    resp = client.post(
        f"/employees/{uid}/punches/add",
        data={"date": "2026-02-05", "time": "09:00"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Added scan" in resp.data

    detail = client.get(f"/employees/{uid}")
    assert detail.status_code == 200
    assert b"helen" in detail.data.lower()


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
