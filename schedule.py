"""Work-schedule rules used to compute net hours and attendance flags.

Default reflects the configured schedule: Monday-Friday, 08:00-18:00, with an
unpaid 12:00-14:00 lunch break (so a full day is 8 net hours). Every value can
be overridden with environment variables (see ``DEFAULT_SCHEDULE`` below).
"""

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta

_MIDNIGHT = time(0, 0)


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

    def compute_hours(self, segments, lunch_override=None):
        """Split a day's work ``segments`` (list of (in, out)) into a
        :class:`HoursBreakdown`.

        Regular hours are in-window time (08-12 and 14-18). Overtime is time
        before the start, time after the end, and the 12:00-14:00 lunch when
        it was *worked* — i.e. no segment left the desk for lunch. ``lunch_
        override`` forces that: ``True`` credits the break, ``False`` deducts
        it, ``None`` (default) decides from the segments. Weekends earn a flat
        credit when present.
        """
        if not segments:
            return HoursBreakdown()
        day = segments[0][0].date()
        if not self.is_workday(day):
            return HoursBreakdown(weekend=round(float(self.weekend_credit_hours), 2))

        def at(t):
            return datetime.combine(day, t)

        day_end = datetime.combine(day, _MIDNIGHT) + timedelta(days=1)
        regular = early = late = auto_break = 0.0
        for clock_in, clock_out in segments:
            regular += self._overlap(clock_in, clock_out, at(self.start), at(self.break_start))
            regular += self._overlap(clock_in, clock_out, at(self.break_end), at(self.end))
            early += self._overlap(clock_in, clock_out, at(_MIDNIGHT), at(self.start))
            late += self._overlap(clock_in, clock_out, at(self.end), day_end)
            auto_break += self._overlap(clock_in, clock_out, at(self.break_start),
                                        at(self.break_end))

        if lunch_override is True:
            first_in, last_out = segments[0][0], segments[-1][1]
            break_worked = self._overlap(first_in, last_out, at(self.break_start),
                                         at(self.break_end))
        elif lunch_override is False:
            break_worked = 0.0
        else:
            break_worked = auto_break

        return HoursBreakdown(
            regular=round(regular, 2), early=round(early, 2),
            break_worked=round(break_worked, 2), late=round(late, 2),
        )

    def net_hours(self, start_dt, end_dt, lunch_override=None):
        """Total paid hours for a single continuous span (regular + extra)."""
        return self.compute_hours([(start_dt, end_dt)], lunch_override).total

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
