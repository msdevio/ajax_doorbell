"""The list of visits (rings and people at the door) to replay from the NVR.

Only times are kept, in Home Assistant's storage: the video stays on the NVR.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

# Activity closer than this to the previous one belongs to the same visit.
MERGE = timedelta(seconds=60)
SAVE_DELAY = 10


@dataclass(slots=True)
class Visit:
    """Someone at the door: from the first detection to the last activity."""

    start: datetime
    end: datetime
    ring: bool
    person: bool

    @property
    def id(self) -> str:
        """Return a stable id: the start time."""
        return self.start.strftime("%Y%m%dT%H%M%SZ")

    @property
    def kind(self) -> str:
        """Return "ring" when someone rang, else "person"."""
        return "ring" if self.ring else "person"

    def as_dict(self) -> dict[str, Any]:
        """Return the stored form."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "ring": self.ring,
            "person": self.person,
        }


class VisitLog:
    """Visits of the last few days, newest last."""

    def __init__(self, hass: HomeAssistant, entry_id: str, retention_days: int) -> None:
        """Prepare the log; async_load reads what was stored."""
        self._store: Store[list[dict[str, Any]]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.visits")
        self._retention = timedelta(days=retention_days)
        self.visits: list[Visit] = []
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        """Read the stored visits."""
        for item in await self._store.async_load() or []:
            start = dt_util.parse_datetime(item.get("start", ""))
            end = dt_util.parse_datetime(item.get("end", "")) or start
            if start is not None and end is not None:
                self.visits.append(
                    Visit(start, end, bool(item.get("ring")), bool(item.get("person")))
                )
        self._prune(dt_util.utcnow())

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener whenever the list changes."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def add(self, kind: str, when: datetime | None = None) -> Visit:
        """Record a ring or a person; activity within a minute extends the last visit."""
        when = when or dt_util.utcnow()
        last = self.visits[-1] if self.visits else None
        if last is not None and when - last.end <= MERGE:
            visit = last
            visit.end = max(visit.end, when)
        else:
            visit = Visit(when, when, False, False)
            self.visits.append(visit)
        if kind == "ring":
            visit.ring = True
        else:
            visit.person = True
        self._prune(when)
        self._store.async_delay_save(self._data, SAVE_DELAY)
        for listener in list(self._listeners):
            listener()
        return visit

    def _prune(self, now: datetime) -> None:
        self.visits = [v for v in self.visits if now - v.start <= self._retention]

    def _data(self) -> list[dict[str, Any]]:
        return [visit.as_dict() for visit in self.visits]

    def get(self, visit_id: str) -> Visit | None:
        """Return a visit by id."""
        return next((v for v in self.visits if v.id == visit_id), None)
