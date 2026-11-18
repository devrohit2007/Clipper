"""Publish-time planning: fixed interval or daily time slots, avoiding times already used on the account."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .utilities import ConfigError

ISO = "%Y-%m-%dT%H:%M:%SZ"


def parse_offset(s: str) -> timezone:
    m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", s.strip())
    if not m:
        raise ConfigError("TZ_OFFSET must look like +05:30")
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(delta if m.group(1) == "+" else -delta)


def parse_slots(s: str) -> list[tuple[int, int]]:
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ConfigError(f"bad time slot {part!r}; use HH:MM like 18:00")
        out.append((int(m.group(1)), int(m.group(2))))
    if not out:
        raise ConfigError("no time slots given")
    return sorted(set(out))


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(ISO)


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, ISO).replace(tzinfo=timezone.utc)


class Scheduler:
    """peek() -> next publish time; commit(t) after a confirmed upload; times never repeat."""

    def __init__(self, cfg, last_iso: Optional[str] = None, now: Optional[datetime] = None):
        self.cfg = cfg
        self._fixed_now = now
        self.last = from_iso(last_iso) if last_iso else None

    @property
    def now(self) -> datetime:
        return self._fixed_now or datetime.now(timezone.utc)

    def _earliest(self) -> datetime:
        e = self.now + timedelta(minutes=self.cfg.publish_start_delay_min)
        if self.last:
            step = timedelta(hours=self.cfg.publish_interval_hours) if self.cfg.schedule_mode == "interval" \
                else timedelta(minutes=1)
            e = max(e, self.last + step)
        return e

    def peek(self) -> str:
        earliest = self._earliest()
        if self.cfg.schedule_mode == "interval":
            return to_iso(earliest)
        tz = parse_offset(self.cfg.tz_offset)
        slots = parse_slots(self.cfg.publish_slots)
        day = earliest.astimezone(tz).date()
        for _ in range(400):
            for h, m in slots:
                cand = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                if cand >= earliest:
                    return to_iso(cand)
            day += timedelta(days=1)
        raise ConfigError("could not find a free publish slot")

    def commit(self, iso: str) -> None:
        t = from_iso(iso)
        if self.last is None or t > self.last:
            self.last = t
