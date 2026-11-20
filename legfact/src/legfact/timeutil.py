"""ISO-8601 timestamp handling with explicit UTC offsets.

All business instants in the warehouse carry an explicit offset.  We never
derive a service date from a message/receipt date: ``service_date`` is an
independent local operating-day field parsed from the message payload.

Instants are compared in absolute (UTC) time while the original offset and
wall-clock text are preserved for provenance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# YYYY-MM-DDTHH:MM:SS(.ffff)? with Z or +HH:MM / -HH:MM  (seconds optional)
_TS_RE = re.compile(
    r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"[T ](?P<h>\d{2}):(?P<mi>\d{2})(?::(?P<s>\d{2})(?:\.(?P<f>\d+))?)?"
    r"(?P<off>Z|[+-]\d{2}:\d{2})$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TimeParseError(ValueError):
    pass


@dataclass(frozen=True)
class OffsetDateTime:
    """An instant with the UTC offset it was originally expressed in."""

    instant: datetime          # tz-aware, normalized to UTC internally
    offset_minutes: int        # e.g. +08:00 -> 480
    original: str              # verbatim text from the source message

    @property
    def utc(self) -> datetime:
        return self.instant.astimezone(timezone.utc)

    @property
    def offset_hhmm(self) -> str:
        sign = "+" if self.offset_minutes >= 0 else "-"
        m = abs(self.offset_minutes)
        return f"{sign}{m // 60:02d}:{m % 60:02d}"

    def iso_utc(self) -> str:
        return self.instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def __str__(self) -> str:
        return self.original

    def __lt__(self, other: "OffsetDateTime") -> bool:
        return self.utc < other.utc

    def __le__(self, other: "OffsetDateTime") -> bool:
        return self.utc <= other.utc

    def __gt__(self, other: "OffsetDateTime") -> bool:
        return self.utc > other.utc

    def __ge__(self, other: "OffsetDateTime") -> bool:
        return self.utc >= other.utc


def parse_offset_datetime(text: str) -> OffsetDateTime:
    """Parse an ISO-8601 string that MUST carry an explicit offset or Z."""
    if not isinstance(text, str):
        raise TimeParseError(f"timestamp is not a string: {text!r}")
    t = text.strip()
    m = _TS_RE.match(t)
    if not m:
        raise TimeParseError(f"not an ISO-8601 timestamp with offset: {text!r}")
    off_raw = m.group("off")
    if off_raw == "Z":
        off_min = 0
    else:
        sign = 1 if off_raw[0] == "+" else -1
        oh, om = off_raw[1:].split(":")
        off_min = sign * (int(oh) * 60 + int(om))
    frac = m.group("f")
    micro = 0
    if frac:
        micro = int((frac + "000000")[:6])
    try:
        wall = datetime(
            int(m.group("y")), int(m.group("mo")), int(m.group("d")),
            int(m.group("h")), int(m.group("mi")),
            int(m.group("s") or 0), micro,
        )
    except ValueError as exc:
        raise TimeParseError(f"invalid calendar date in {text!r}: {exc}") from exc
    tz = timezone(timedelta(minutes=off_min))
    aware = wall.replace(tzinfo=tz)
    return OffsetDateTime(aware.astimezone(timezone.utc), off_min, t)


def parse_service_date(text: str) -> str:
    """Validate and return the independent local operating-day (YYYY-MM-DD)."""
    if not isinstance(text, str) or not _DATE_RE.match(text.strip()):
        raise TimeParseError(f"service_date must be YYYY-MM-DD: {text!r}")
    d = text.strip()
    datetime.strptime(d, "%Y-%m-%d")  # raise on impossible dates
    return d


def utc_instant(text: str) -> datetime:
    return parse_offset_datetime(text).utc


def format_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("refusing to format a naive datetime")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
