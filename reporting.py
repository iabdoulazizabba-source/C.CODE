"""Build pay-period attendance reports over a date range.

For each employee and each day in the range we classify the day as
**present**, **incomplete** (one scan only), **absent** (a scheduled
workday with no scans and not offshore), or **offshore**. Weekends with no
scans are simply skipped. Hours use the schedule's net-hours rule.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional

from schedule import DEFAULT_SCHEDULE

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class DayRecord:
    day: date
    status: str  # present | incomplete | absent | offshore
    clock_in: Optional[datetime] = None
    clock_out: Optional[datetime] = None
    net_hours: float = 0.0
    overtime: float = 0.0
    late: bool = False
    early: bool = False

    @property
    def weekday(self):
        return WEEKDAY_NAMES[self.day.weekday()]


@dataclass
class EmployeeReport:
    user_id: int
    name: str
    days: List[DayRecord] = field(default_factory=list)
    net_hours: float = 0.0
    overtime: float = 0.0
    present_days: int = 0
    incomplete_days: int = 0
    late_days: int = 0
    absent_days: int = 0
    offshore_days: int = 0
    weekend_days: int = 0


def build_report(users, start, end, schedule=DEFAULT_SCHEDULE, today=None):
    """Return an :class:`EmployeeReport` for each user over [start, end].

    Days after ``today`` are not evaluated (the period may be ongoing).
    """
    today = today or date.today()
    cutoff = min(end, today)

    reports = []
    for user in users:
        sessions = {s.date: s for s in user.sessions()}
        report = EmployeeReport(user_id=user.id, name=user.full_name)

        day = start
        while day <= cutoff:
            session = sessions.get(day)
            is_weekend = not schedule.is_workday(day)
            if session is not None and is_weekend:
                # Weekend present -> flat extra credit, all overtime.
                report.days.append(DayRecord(
                    day, "weekend", session.clock_in, session.clock_out,
                    net_hours=session.net_hours, overtime=session.overtime_hours,
                ))
                report.weekend_days += 1
                report.net_hours += session.net_hours
                report.overtime += session.overtime_hours
            elif session is not None and session.is_open:
                report.days.append(DayRecord(day, "incomplete", session.clock_in,
                                             None, late=session.is_late))
                report.incomplete_days += 1
                if session.is_late:
                    report.late_days += 1
            elif session is not None:
                report.days.append(DayRecord(
                    day, "present", session.clock_in, session.clock_out,
                    net_hours=session.net_hours, overtime=session.overtime_hours,
                    late=session.is_late, early=session.is_early_leave,
                ))
                report.present_days += 1
                report.net_hours += session.net_hours
                report.overtime += session.overtime_hours
                if session.is_late:
                    report.late_days += 1
            elif user.is_offshore_on(day):
                report.days.append(DayRecord(day, "offshore"))
                report.offshore_days += 1
            elif schedule.is_workday(day):
                report.days.append(DayRecord(day, "absent"))
                report.absent_days += 1
            # else: weekend with no work -> not recorded
            day += timedelta(days=1)

        report.net_hours = round(report.net_hours, 2)
        report.overtime = round(report.overtime, 2)
        reports.append(report)

    return reports


@dataclass
class TodayRow:
    user_id: int
    name: str
    status: str  # on_site | checked_out | not_arrived | offshore
    first_in: Optional[datetime] = None
    last_out: Optional[datetime] = None
    late: bool = False
    net_hours: float = 0.0


@dataclass
class TodayBoard:
    day: date
    is_workday: bool
    rows: List[TodayRow] = field(default_factory=list)
    on_site: int = 0
    checked_out: int = 0
    not_arrived: int = 0
    offshore: int = 0
    late: int = 0


# Order statuses so "who's here / who's missing" reads top-down.
_TODAY_ORDER = {"on_site": 0, "not_arrived": 1, "checked_out": 2, "offshore": 3}


def build_today(users, schedule=DEFAULT_SCHEDULE, today=None):
    """Snapshot of where each active user stands *today*."""
    today = today or date.today()
    is_workday = schedule.is_workday(today)
    board = TodayBoard(day=today, is_workday=is_workday)

    for user in users:
        if user.is_offshore_on(today):
            board.rows.append(TodayRow(user.id, user.full_name, "offshore"))
            board.offshore += 1
            continue

        session = next((s for s in user.sessions() if s.date == today), None)
        if session is None:
            if is_workday:
                board.rows.append(TodayRow(user.id, user.full_name, "not_arrived"))
                board.not_arrived += 1
            continue

        if session.is_open:
            board.rows.append(TodayRow(
                user.id, user.full_name, "on_site",
                first_in=session.clock_in, late=session.is_late,
            ))
            board.on_site += 1
        else:
            board.rows.append(TodayRow(
                user.id, user.full_name, "checked_out",
                first_in=session.clock_in, last_out=session.clock_out,
                late=session.is_late, net_hours=session.net_hours,
            ))
            board.checked_out += 1
        if session.is_late:
            board.late += 1

    board.rows.sort(key=lambda r: (_TODAY_ORDER.get(r.status, 9), r.name))
    return board
