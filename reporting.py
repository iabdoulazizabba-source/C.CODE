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
            if session is not None and session.is_open:
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
