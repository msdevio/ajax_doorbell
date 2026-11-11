"""What the NVR's cameras detected: times only, to find and replay the moments.

The video stays on the NVR. This keeps, per camera, when something moved or
someone passed, for the number of days chosen in the NVR's options.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

# Activity closer than this to the previous detection extends it.
MERGE = timedelta(seconds=30)
SAVE_DELAY = 30
# A busy street must not grow the store without bound.
MAX_PER_CAMERA = 2000


@dataclass(slots=True)
class Detection:
    """One stretch of activity in front of a camera."""

    start: datetime
    end: datetime
    kinds: set[str] = field(default_factory=set)  # motion, person, vehicle, pet

    @property
    def id(self) -> str:
        """Return a stable id: the start time."""
        return self.start.strftime("%Y%m%dT%H%M%SZ")

    def as_dict(self) -> dict[str, Any]:
        """Return the stored form."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "kinds": sorted(self.kinds),
        }


class DetectionLog:
    """Detections of the last few days, per camera, oldest first."""

    def __init__(self, hass: HomeAssistant, entry_id: str, retention_days: int) -> None:
        """Prepare the log; async_load reads what was stored."""
        self._store: Store[dict[str, list[dict[str, Any]]]] = Store(
            hass, 1, f"{DOMAIN}.{entry_id}.detections"
        )
        self.retention = timedelta(days=retention_days)
        self.cameras: dict[str, list[Detection]] = {}
        self._listeners: list[Callable[[str], None]] = []

    async def async_load(self) -> None:
        """Read the stored detections."""
        for group, items in (await self._store.async_load() or {}).items():
            detections = []
            for item in items:
                start = dt_util.parse_datetime(item.get("start", ""))
                end = dt_util.parse_datetime(item.get("end", "")) or start
                if start is not None and end is not None:
                    detections.append(Detection(start, end, set(item.get("kinds", []))))
            self.cameras[group] = detections
        self._prune(dt_util.utcnow())

    @callback
    def async_add_listener(self, listener: Callable[[str], None]) -> CALLBACK_TYPE:
        """Call listener(camera) whenever a camera's list changes."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def add(self, group: str, kind: str, when: datetime | None = None) -> Detection:
        """Record a detection; activity within MERGE extends the last one."""
        when = when or dt_util.utcnow()
        items = self.cameras.setdefault(group, [])
        last = items[-1] if items else None
        if last is not None and when - last.end <= MERGE:
            last.end = max(last.end, when)
            last.kinds.add(kind)
            detection = last
        else:
            detection = Detection(when, when, {kind})
            items.append(detection)
        self._changed(group, when)
        return detection

    @callback
    def extend(self, group: str, when: datetime | None = None) -> None:
        """Activity went on (or just stopped): the last detection lasts until now."""
        when = when or dt_util.utcnow()
        items = self.cameras.get(group)
        if items and when - items[-1].end <= MERGE and when > items[-1].end:
            items[-1].end = when
            self._changed(group, when)

    async def async_remove(self) -> None:
        """Delete the stored journal."""
        await self._store.async_remove()

    def get(self, group: str, detection_id: str) -> Detection | None:
        """Return a detection by id."""
        return next((d for d in self.cameras.get(group, []) if d.id == detection_id), None)

    def _changed(self, group: str, now: datetime) -> None:
        self._prune(now)
        self._store.async_delay_save(self._data, SAVE_DELAY)
        for listener in list(self._listeners):
            listener(group)

    def _prune(self, now: datetime) -> None:
        for group, items in self.cameras.items():
            kept = [d for d in items if now - d.start <= self.retention]
            self.cameras[group] = kept[-MAX_PER_CAMERA:]

    def _data(self) -> dict[str, list[dict[str, Any]]]:
        return {
            group: [d.as_dict() for d in items] for group, items in self.cameras.items() if items
        }
