"""Cadence dialect normalisation.

Sources do not share a cadence language, and pretending otherwise
corrupts data. Each normaliser here maps one dialect to an iCalendar
RRULE where the mapping is exact, and returns ``None`` where it is not.
Returning ``None`` is a feature: a relative timer (fires N minutes after
the unit was last active) and a self-chaining job (next run decided when
the previous run finishes) have no honest RRULE, so the series records
the dialect, keeps the original expression, and the projector works from
the source's own next-run field instead of an expansion.

The original expression is always preserved alongside the RRULE; see
:class:`CadenceResult`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class CadenceResult:
    dialect: str
    expr: Optional[str]  # the original expression, exactly as the source states it
    rrule: Optional[str]  # normalised RRULE, or None when no exact mapping exists


# ---------------------------------------------------------------------------
# Shared field helpers
# ---------------------------------------------------------------------------

def _expand_field(spec: str, lo: int, hi: int) -> Optional[list[int]]:
    """Expand one cron-style field spec to a sorted list of ints.

    Supports ``*``, single values, comma lists, ranges, and steps
    (``*/n``, ``a-b/n``, ``a/n``). Returns None on anything else.
    """
    values: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            return None
        step = 1
        if "/" in token:
            base, _, step_text = token.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                return None
            step = int(step_text)
        else:
            base = token
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return None
            start, end = int(a), int(b)
        elif base.isdigit():
            start = int(base)
            end = hi if "/" in token else start
        else:
            return None
        if start < lo or end > hi or start > end:
            return None
        values.update(range(start, end + 1, step))
    return sorted(values)


def _full_set(lo: int, hi: int) -> list[int]:
    return list(range(lo, hi + 1))


def _join(values: list[int]) -> str:
    return ",".join(str(v) for v in values)


_DAY_CODES = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]  # index: 0 = Sunday
_DAY_NAME_TO_NUM = {
    "SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6,
}
_MONTH_NAME_TO_NUM = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _substitute_names(spec: str, mapping: dict[str, int]) -> str:
    def repl(match: re.Match) -> str:
        return str(mapping[match.group(0).upper()])

    pattern = r"\b(" + "|".join(mapping.keys()) + r")\b"
    return re.sub(pattern, repl, spec, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------

def cron_to_rrule(expr: str) -> Optional[str]:
    """Normalise a standard 5-field cron expression to an RRULE.

    Returns None for constructs RRULE cannot express exactly, most
    importantly a day-of-month and day-of-week both restricted (cron
    fires when either matches; RRULE would require both).
    """
    fields = expr.split()
    if len(fields) != 5:
        return None
    minute_s, hour_s, dom_s, month_s, dow_s = fields
    month_s = _substitute_names(month_s, _MONTH_NAME_TO_NUM)
    dow_s = _substitute_names(dow_s, _DAY_NAME_TO_NUM)

    minutes = _expand_field(minute_s, 0, 59)
    hours = _expand_field(hour_s, 0, 23)
    dom = _expand_field(dom_s, 1, 31)
    months = _expand_field(month_s, 1, 12)
    dow_raw = _expand_field(dow_s, 0, 7)
    if None in (minutes, hours, dom, months, dow_raw):
        return None
    dow = sorted({0 if d == 7 else d for d in dow_raw})

    minutes_full = minutes == _full_set(0, 59)
    hours_full = hours == _full_set(0, 23)
    dom_full = dom == _full_set(1, 31)
    months_full = months == _full_set(1, 12)
    dow_full = dow == _full_set(0, 6)

    if not dom_full and not dow_full:
        return None  # cron OR-semantics; RRULE is AND. Not exact.

    def filters() -> list[str]:
        out = []
        if not months_full:
            out.append("BYMONTH=" + _join(months))
        if not dom_full:
            out.append("BYMONTHDAY=" + _join(dom))
        if not dow_full:
            out.append("BYDAY=" + ",".join(_DAY_CODES[d] for d in dow))
        return out

    minute_step = re.fullmatch(r"\*/(\d+)", minute_s)
    hour_step = re.fullmatch(r"\*/(\d+)", hour_s)

    if minute_step and hours_full and dom_full and dow_full and months_full:
        return f"FREQ=MINUTELY;INTERVAL={minute_step.group(1)}"
    if hour_step:
        if minutes_full:
            return None  # every minute of every nth hour: not exact in RRULE
        parts = [f"FREQ=HOURLY;INTERVAL={hour_step.group(1)}"]
        parts += filters()
        parts.append("BYMINUTE=" + _join(minutes))
        parts.append("BYSECOND=0")
        return ";".join(parts)
    if minutes_full:
        parts = ["FREQ=MINUTELY;INTERVAL=1"]
        if not hours_full:
            parts.append("BYHOUR=" + _join(hours))
        parts += filters()
        return ";".join(parts)

    # Minute is a restricted set from here on.
    if not dom_full:
        freq = "FREQ=YEARLY" if not months_full else "FREQ=MONTHLY"
        parts = [freq]
        if not months_full:
            parts.append("BYMONTH=" + _join(months))
        parts.append("BYMONTHDAY=" + _join(dom))
    elif not dow_full:
        parts = ["FREQ=WEEKLY", "BYDAY=" + ",".join(_DAY_CODES[d] for d in dow)]
        if not months_full:
            parts.append("BYMONTH=" + _join(months))
    elif not months_full:
        parts = ["FREQ=YEARLY", "BYMONTH=" + _join(months)]
    elif not hours_full or len(minutes) > 0:
        parts = ["FREQ=DAILY"]
    else:  # pragma: no cover - unreachable, kept for clarity
        parts = ["FREQ=DAILY"]
    if not hours_full:
        parts.append("BYHOUR=" + _join(hours))
    parts.append("BYMINUTE=" + _join(minutes))
    parts.append("BYSECOND=0")
    return ";".join(parts)


_CRON_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}


def cron_macro_to_rrule(expr: str) -> Optional[str]:
    """Normalise an @-style cron macro. ``@reboot`` has no RRULE."""
    macro = expr.strip().lower()
    if macro == "@reboot":
        return None
    expanded = _CRON_MACROS.get(macro)
    return cron_to_rrule(expanded) if expanded else None


# ---------------------------------------------------------------------------
# systemd calendar expressions
# ---------------------------------------------------------------------------

_SYSTEMD_KEYWORDS = {
    "minutely": "*-*-* *:*:00",
    "hourly": "*-*-* *:00:00",
    "daily": "*-*-* 00:00:00",
    "midnight": "*-*-* 00:00:00",
    "weekly": "Mon *-*-* 00:00:00",
    "monthly": "*-*-01 00:00:00",
    "yearly": "*-01-01 00:00:00",
    "annually": "*-01-01 00:00:00",
}

_DOW_TOKEN = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(\.\.(Mon|Tue|Wed|Thu|Fri|Sat|Sun))?$",
    re.IGNORECASE,
)


def _parse_dow_set(spec: str) -> Optional[list[int]]:
    days: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        m = _DOW_TOKEN.match(token)
        if not m:
            return None
        first = _DAY_NAME_TO_NUM[m.group(1)[:3].upper()]
        if m.group(3):
            last = _DAY_NAME_TO_NUM[m.group(3)[:3].upper()]
            cur = first
            while True:
                days.add(cur)
                if cur == last:
                    break
                cur = (cur + 1) % 7
        else:
            days.add(first)
    return sorted(days)


def systemd_calendar_to_rrule(expr: str) -> Optional[str]:
    """Normalise a systemd calendar expression to an RRULE.

    Covers the common subset: the keyword forms (``daily``, ``weekly``,
    ...), ``*-*-* HH:MM:SS``, weekday forms (``Mon *-*-* ...``,
    ``Mon..Fri``, comma lists), monthly-by-day forms (``*-*-01 ...``),
    yearly forms, and wildcard hour/minute combinations. Returns None
    for anything outside the subset (restricted years, ``~`` last-day
    forms, relative timer text) rather than guessing.
    """
    text = expr.strip()
    if not text or "OnBoot" in text or "OnUnit" in text or "OnActive" in text:
        return None
    text = _SYSTEMD_KEYWORDS.get(text.lower(), text)

    tokens = text.split()
    dow: Optional[list[int]] = None
    if len(tokens) == 3:
        dow = _parse_dow_set(tokens[0])
        if dow is None:
            return None
        date_s, time_s = tokens[1], tokens[2]
    elif len(tokens) == 2:
        date_s, time_s = tokens
    else:
        return None

    date_parts = date_s.split("-")
    time_parts = time_s.split(":")
    if len(date_parts) != 3 or len(time_parts) not in (2, 3):
        return None
    year_s, month_s, day_s = date_parts
    if year_s != "*":
        return None  # a pinned year is a one-off in disguise, not a series
    if "~" in day_s:
        return None
    months = _expand_field(month_s, 1, 12) if month_s != "*" else _full_set(1, 12)
    days = _expand_field(day_s, 1, 31) if day_s != "*" else _full_set(1, 31)
    hours = _expand_field(time_parts[0], 0, 23) if time_parts[0] != "*" else _full_set(0, 23)
    minutes = (
        _expand_field(time_parts[1], 0, 59) if time_parts[1] != "*" else _full_set(0, 59)
    )
    if len(time_parts) == 3:
        seconds = (
            _expand_field(time_parts[2], 0, 59)
            if time_parts[2] != "*"
            else _full_set(0, 59)
        )
    else:
        seconds = [0]
    if None in (months, days, hours, minutes, seconds):
        return None

    months_full = months == _full_set(1, 12)
    days_full = days == _full_set(1, 31)
    hours_full = hours == _full_set(0, 23)
    minutes_full = minutes == _full_set(0, 59)
    seconds_full = seconds == _full_set(0, 59)

    if dow is not None:
        parts = ["FREQ=WEEKLY", "BYDAY=" + ",".join(_DAY_CODES[d] for d in dow)]
        if not months_full:
            parts.append("BYMONTH=" + _join(months))
        if not days_full:
            parts.append("BYMONTHDAY=" + _join(days))
    elif not days_full:
        parts = ["FREQ=YEARLY" if not months_full else "FREQ=MONTHLY"]
        if not months_full:
            parts.append("BYMONTH=" + _join(months))
        parts.append("BYMONTHDAY=" + _join(days))
    elif not months_full:
        parts = ["FREQ=YEARLY", "BYMONTH=" + _join(months)]
    elif hours_full and minutes_full and seconds_full:
        return "FREQ=MINUTELY;INTERVAL=1"
    elif minutes_full and not hours_full:
        parts = ["FREQ=MINUTELY", "BYHOUR=" + _join(hours)]
        if not seconds_full:
            parts.append("BYSECOND=" + _join(seconds))
        return ";".join(parts)
    elif hours_full and not minutes_full:
        parts = ["FREQ=HOURLY", "BYMINUTE=" + _join(minutes)]
        if not seconds_full:
            parts.append("BYSECOND=" + _join(seconds))
        return ";".join(parts)
    else:
        parts = ["FREQ=DAILY"]
    if dow is None and days_full and months_full:
        pass  # daily family: BYHOUR/BYMINUTE/BYSECOND appended below
    if "FREQ=DAILY" in parts or (dow is not None) or not days_full or not months_full:
        if not hours_full:
            parts.append("BYHOUR=" + _join(hours))
        if not minutes_full:
            parts.append("BYMINUTE=" + _join(minutes))
        elif minutes_full and not seconds_full:
            parts.append("BYMINUTE=" + _join(minutes))
        if not seconds_full:
            parts.append("BYSECOND=" + _join(seconds))
        elif len(seconds) == 1:
            parts.append("BYSECOND=" + str(seconds[0]))
    return ";".join(parts)


_RELATIVE_MARKERS = ("OnBoot", "OnUnitActive", "OnUnitInactive", "OnActive", "OnStartup")


def classify_systemd_timer(on_calendar: Optional[str], raw_text: str = "") -> str:
    """Decide the dialect of a systemd timer: calendar when it carries an
    OnCalendar expression, relative when its triggers are boot/activity
    based (those expand from last activation, never from a calendar)."""
    if on_calendar:
        return "systemd-calendar"
    haystack = raw_text or ""
    if any(marker in haystack for marker in _RELATIVE_MARKERS):
        return "systemd-relative"
    if re.search(r"every\s+\S+\s*\(", haystack):
        return "systemd-relative"
    return "systemd-relative"


def systemd_relative_to_rrule(expr: str) -> Optional[str]:
    """Relative timers have no RRULE. Always None, on purpose."""
    return None


# ---------------------------------------------------------------------------
# Interval strings ("every 30m", "daily at 09:07:00")
# ---------------------------------------------------------------------------

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800,
}


def interval_seconds_to_rrule(total_seconds: int) -> Optional[str]:
    if total_seconds <= 0:
        return None
    if total_seconds % 604800 == 0:
        return f"FREQ=WEEKLY;INTERVAL={total_seconds // 604800}"
    if total_seconds % 86400 == 0:
        return f"FREQ=DAILY;INTERVAL={total_seconds // 86400}"
    if total_seconds % 3600 == 0:
        return f"FREQ=HOURLY;INTERVAL={total_seconds // 3600}"
    if total_seconds % 60 == 0:
        return f"FREQ=MINUTELY;INTERVAL={total_seconds // 60}"
    return f"FREQ=SECONDLY;INTERVAL={total_seconds}"


def interval_hours_to_rrule(hours) -> Optional[str]:
    """RRULE for a cadence stated in hours (a registry column shape)."""
    if hours is None:
        return None
    try:
        seconds = int(round(float(hours) * 3600))
    except (TypeError, ValueError):
        return None
    return interval_seconds_to_rrule(seconds)


_AT_TIME = re.compile(
    r"^(daily|weekly|monthly)(?:\s+on\s+([A-Za-z]{3,9}))?\s+at\s+"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?$",
    re.IGNORECASE,
)
_EVERY = re.compile(r"^every\s+(\d+)?\s*([A-Za-z]+)$", re.IGNORECASE)


def interval_to_rrule(expr: str) -> Optional[str]:
    """Normalise a human interval string to an RRULE.

    Handles ``every 30m``, ``every 1h``, ``every hour``, and clock forms
    ``daily at 09:07:00`` / ``weekly at 09:07`` (optionally
    ``weekly on Tue at 09:07``).
    """
    text = expr.strip()
    m = _AT_TIME.match(text)
    if m:
        freq = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY"}[
            m.group(1).lower()
        ]
        parts = [f"FREQ={freq}"]
        if m.group(2):
            key = m.group(2)[:3].upper()
            if key not in _DAY_NAME_TO_NUM:
                return None
            parts.append("BYDAY=" + _DAY_CODES[_DAY_NAME_TO_NUM[key]])
        parts.append(f"BYHOUR={int(m.group(3))}")
        parts.append(f"BYMINUTE={int(m.group(4))}")
        parts.append(f"BYSECOND={int(m.group(5) or 0)}")
        return ";".join(parts)
    m = _EVERY.match(text)
    if m:
        count = int(m.group(1) or 1)
        unit = m.group(2).lower()
        if unit not in _UNIT_SECONDS:
            return None
        return interval_seconds_to_rrule(count * _UNIT_SECONDS[unit])
    bare = {"hourly": "FREQ=HOURLY", "daily": "FREQ=DAILY",
            "weekly": "FREQ=WEEKLY", "monthly": "FREQ=MONTHLY"}
    return bare.get(text.lower())


# ---------------------------------------------------------------------------
# YAML cadence words
# ---------------------------------------------------------------------------

_YAML_WORDS = {
    "hourly": "FREQ=HOURLY",
    "daily": "FREQ=DAILY",
    "weekly": "FREQ=WEEKLY",
    "monthly": "FREQ=MONTHLY",
    "quarterly": "FREQ=MONTHLY;INTERVAL=3",
    "yearly": "FREQ=YEARLY",
    "annual": "FREQ=YEARLY",
    "annually": "FREQ=YEARLY",
}


def yaml_cadence_to_rrule(word: str) -> Optional[str]:
    """Normalise a YAML cadence word to an RRULE.

    Plain words map directly (quarterly is MONTHLY with INTERVAL=3).
    Anything richer falls through to the interval and cron normalisers,
    so a YAML file may also carry ``every 6h`` or a cron expression.
    ``none`` honestly returns None.
    """
    if word is None:
        return None
    text = str(word).strip()
    if not text or text.lower() == "none":
        return None
    hit = _YAML_WORDS.get(text.lower())
    if hit:
        return hit
    return interval_to_rrule(text) or cron_to_rrule(text)


def self_chain_to_rrule(expr: Optional[str]) -> Optional[str]:
    """Self-chaining jobs have no RRULE: the next run is decided when the
    previous run completes. Always None, on purpose."""
    return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def normalize(dialect: str, expr: Optional[str]) -> CadenceResult:
    """Normalise ``expr`` under ``dialect``, preserving the original."""
    if expr is None:
        return CadenceResult(dialect=dialect, expr=None, rrule=None)
    if dialect == "cron":
        rrule = cron_macro_to_rrule(expr) if expr.strip().startswith("@") else cron_to_rrule(expr)
    elif dialect == "systemd-calendar":
        rrule = systemd_calendar_to_rrule(expr)
    elif dialect == "systemd-relative":
        rrule = systemd_relative_to_rrule(expr)
    elif dialect == "interval":
        rrule = interval_to_rrule(expr)
    elif dialect == "self-chain":
        rrule = self_chain_to_rrule(expr)
    elif dialect == "rrule":
        rrule = expr.strip() or None
    else:  # "none" and anything unrecognised
        rrule = None
    return CadenceResult(dialect=dialect, expr=expr, rrule=rrule)
