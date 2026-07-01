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


def round_hours(value):
    """Round hours to the nearest whole hour (half up). Display only."""
    return int(float(value) + 0.5)


@dataclass
class HoursBreakdown:
    """How a single day's worked hours split into regular vs extra."""

    regular: float = 0.0       # in-window weekday hours (08-12, 14-18), <= 8
    early: float = 0.0         # worked before the start time
    break_worked: float = 0.0  # worked through the 12-14 lunch break
    late: float = 0.0          # worked after the end time
    weekend: float = 0.0       # flat credit for a Sat/Sun present

    @property
    def extra(self):
        return round(self.early + self.break_worked + self.late + self.weekend, 2)

    @property
    def total(self):
        return round(self.regular + self.extra, 2)


@dataclass(frozen=True)
class Schedule:
    work_days: frozenset = frozenset({0, 1, 2, 3, 4})  # Mon=0 .. Sun=6
    start: time = time(8, 0)
    end: time = time(18, 0)
    break_start: time = time(12, 0)
    break_end: time = time(14, 0)
    late_grace_minutes: int = 10
    weekend_credit_hours: float = 10.0  # flat extra for a Sat/Sun present

    # --- day classification ---
    def is_workday(self, day):
        return day.weekday() in self.work_days

    @property
    def break_hours(self):
        return (_mins(self.break_end) - _mins(self.break_start)) / 60

    @property
    def expected_hours(self):
        """Regular hours in a full scheduled day (window minus the break)."""
        gross = (_mins(self.end) - _mins(self.start)) / 60
        return round(gross - self.break_hours, 2)

    # --- hours ---
    @staticmethod
    def _overlap(start_dt, end_dt, win_start, win_end):
        secs = (min(end_dt, win_end) - max(start_dt, win_start)).total_seconds()
        return max(0.0, secs / 3600)

    def compute_hours(self, clock_in, clock_out, lunch_worked=False):
        """Split a day's attendance into a :class:`HoursBreakdown`.

        Weekends (non-workdays) earn a flat ``weekend_credit_hours`` when the
        person is present. On weekdays, regular hours are the in-window time
        (08-12 and 14-18); extra hours are time before the start, time after
        the end, and the lunch break *only when ``lunch_worked`` is set* (an
        admin marks the day). Otherwise the lunch is unpaid and deducted.
        """
        day = clock_in.date()
        if not self.is_workday(day):
            return HoursBreakdown(weekend=round(float(self.weekend_credit_hours), 2))
        if clock_out is None:
            return HoursBreakdown()  # single scan, incomplete

        def at(t):
            return datetime.combine(day, t)

        regular = round(
            self._overlap(clock_in, clock_out, at(self.start), at(self.break_start))
            + self._overlap(clock_in, clock_out, at(self.break_end), at(self.end)),
            2,
        )
        early = round(self._overlap(clock_in, clock_out, at(time(0, 0)), at(self.start)), 2)
        late = round((clock_out - max(clock_in, at(self.end))).total_seconds() / 3600, 2)
        late = max(0.0, late)

        break_worked = (
            round(self._overlap(clock_in, clock_out, at(self.break_start),
                                at(self.break_end)), 2)
            if lunch_worked else 0.0
        )
        return HoursBreakdown(regular=regular, early=early,
                              break_worked=break_worked, late=late)

    def net_hours(self, start_dt, end_dt, lunch_worked=False):
        """Total paid hours for the day (regular + extra)."""
        return self.compute_hours(start_dt, end_dt, lunch_worked).total

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
    weekend_credit_hours=float(os.environ.get("WEEKEND_CREDIT_HOURS", "10")),
)
