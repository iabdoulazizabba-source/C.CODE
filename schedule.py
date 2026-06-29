"""Work-schedule rules used to compute net hours and attendance flags.

Default reflects the configured schedule: Monday-Friday, 08:00-18:00, with an
unpaid 12:00-14:00 lunch break (so a full day is 8 net hours). Every value can
be overridden with environment variables (see ``DEFAULT_SCHEDULE`` below).
"""

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta


def _parse_time(value, default):
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return default


def _mins(t):
    return t.hour * 60 + t.minute


@dataclass(frozen=True)
class Schedule:
    work_days: frozenset = frozenset({0, 1, 2, 3, 4})  # Mon=0 .. Sun=6
    start: time = time(8, 0)
    end: time = time(18, 0)
    break_start: time = time(12, 0)
    break_end: time = time(14, 0)
    late_grace_minutes: int = 10

    # --- day classification ---
    def is_workday(self, day):
        return day.weekday() in self.work_days

    @property
    def break_hours(self):
        return (_mins(self.break_end) - _mins(self.break_start)) / 60

    @property
    def expected_hours(self):
        """Net hours in a full scheduled day (gross span minus the break)."""
        gross = (_mins(self.end) - _mins(self.start)) / 60
        return round(gross - self.break_hours, 2)

    # --- hours ---
    def break_overlap_hours(self, start_dt, end_dt):
        """Hours of the lunch break that fall inside [start_dt, end_dt]."""
        b_start = datetime.combine(start_dt.date(), self.break_start)
        b_end = datetime.combine(start_dt.date(), self.break_end)
        overlap = (min(end_dt, b_end) - max(start_dt, b_start)).total_seconds()
        return max(0.0, overlap / 3600)

    def net_hours(self, start_dt, end_dt):
        gross = (end_dt - start_dt).total_seconds() / 3600
        return round(max(0.0, gross - self.break_overlap_hours(start_dt, end_dt)), 2)

    def overtime_hours(self, net):
        return round(max(0.0, net - self.expected_hours), 2)

    # --- flags ---
    def _late_limit(self, day):
        return datetime.combine(day, self.start) + timedelta(
            minutes=self.late_grace_minutes
        )

    def is_late(self, first_in):
        return first_in > self._late_limit(first_in.date())

    def lateness_minutes(self, first_in):
        limit = self._late_limit(first_in.date())
        if first_in <= limit:
            return 0
        return int((first_in - limit).total_seconds() // 60)

    def is_early_leave(self, last_out):
        limit = datetime.combine(last_out.date(), self.end) - timedelta(
            minutes=self.late_grace_minutes
        )
        return last_out < limit


DEFAULT_SCHEDULE = Schedule(
    start=_parse_time(os.environ.get("WORK_START"), time(8, 0)),
    end=_parse_time(os.environ.get("WORK_END"), time(18, 0)),
    break_start=_parse_time(os.environ.get("BREAK_START"), time(12, 0)),
    break_end=_parse_time(os.environ.get("BREAK_END"), time(14, 0)),
    late_grace_minutes=int(os.environ.get("LATE_GRACE_MINUTES", "10")),
)
