"""Call state machine, modelled on the Ajax app's incoming-call screen.

idle -> ringing (someone pressed the button)
ringing -> in_call (a dashboard or phone answered)
ringing -> idle (dismissed, or missed when nobody answered in time)
in_call -> idle (hung up, or the safety timeout fired)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import EVENT_CALL


class CallState(StrEnum):
    """Where the current call is."""

    IDLE = "idle"
    RINGING = "ringing"
    IN_CALL = "in_call"


class CallManager:
    """Track one doorbell's call and tell listeners and the event bus."""

    def __init__(
        self,
        hass: HomeAssistant,
        event_data: Callable[[], dict[str, Any]],
        ring_duration: float,
        call_timeout: float,
    ) -> None:
        """Initialize an idle call."""
        self._hass = hass
        self._event_data = event_data
        self.ring_duration = ring_duration
        self.call_timeout = call_timeout
        self.state = CallState.IDLE
        self.ring_started: datetime | None = None
        self.answered_by: str | None = None
        self.answered_at: datetime | None = None
        self.last_ring: datetime | None = None
        self.last_missed: datetime | None = None
        self.missed_calls = 0
        self._ring_timer: CALLBACK_TYPE | None = None
        self._call_timer: CALLBACK_TYPE | None = None
        self._listeners: list[Callable[[], None]] = []

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener on every change; return the function that removes it."""
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners.remove(listener)

        return remove

    @callback
    def _changed(self, event: str, **data: Any) -> None:
        self._hass.bus.async_fire(
            EVENT_CALL, {"type": event, "state": self.state, **self._event_data(), **data}
        )
        for listener in list(self._listeners):
            listener()

    def _cancel(self, attr: str) -> None:
        if (cancel := getattr(self, attr)) is not None:
            cancel()
            setattr(self, attr, None)

    @callback
    def ring(self) -> None:
        """Someone pressed the button."""
        now = dt_util.utcnow()
        self.last_ring = now
        if self.state is CallState.IN_CALL:
            # A second press during the conversation: tell automations, keep talking.
            self._changed("ring", during_call=True)
            return
        if self.state is CallState.IDLE:
            self.ring_started = now
            self.answered_by = self.answered_at = None
        self.state = CallState.RINGING
        self._cancel("_ring_timer")
        self._ring_timer = async_call_later(self._hass, self.ring_duration, self._missed)
        self._changed("ring", during_call=False)

    @callback
    def answer(self, answered_by: str | None = None) -> None:
        """A screen picked up; answering again during a call keeps it alive."""
        after_ring = self.state is CallState.RINGING
        already = self.state is CallState.IN_CALL
        self._cancel("_ring_timer")
        self._cancel("_call_timer")
        self._call_timer = async_call_later(self._hass, self.call_timeout, self._expired)
        if already and answered_by in (None, self.answered_by):
            return
        if not already and not after_ring:
            self.ring_started = None
        self.state = CallState.IN_CALL
        self.answered_by = answered_by or "Home Assistant"
        self.answered_at = dt_util.utcnow()
        self._changed("answered", answered_by=self.answered_by, after_ring=after_ring)

    @callback
    def hang_up(self) -> None:
        """End the conversation; while it rings, decline it."""
        if self.state is CallState.RINGING:
            self.dismiss()
            return
        if self.state is not CallState.IN_CALL:
            return
        self._cancel("_call_timer")
        self.state = CallState.IDLE
        self._changed("ended", reason="hang_up")

    @callback
    def dismiss(self) -> None:
        """Stop the ringing without answering."""
        if self.state is not CallState.RINGING:
            return
        self._cancel("_ring_timer")
        self.state = CallState.IDLE
        self._changed("dismissed")

    @callback
    def clear_missed(self) -> None:
        """Reset the missed-call counter."""
        self.missed_calls = 0
        self._changed("missed_cleared")

    @callback
    def restore(
        self,
        missed_calls: int | None = None,
        last_ring: datetime | None = None,
        last_missed: datetime | None = None,
    ) -> None:
        """Bring back values saved before a restart."""
        if missed_calls is not None:
            self.missed_calls = missed_calls
        self.last_ring = self.last_ring or last_ring
        self.last_missed = self.last_missed or last_missed

    @callback
    def _missed(self, _now: datetime) -> None:
        self._ring_timer = None
        if self.state is not CallState.RINGING:
            return
        self.state = CallState.IDLE
        self.missed_calls += 1
        self.last_missed = dt_util.utcnow()
        self._changed("missed")

    @callback
    def _expired(self, _now: datetime) -> None:
        self._call_timer = None
        if self.state is CallState.IN_CALL:
            self.state = CallState.IDLE
            self._changed("ended", reason="timeout")

    @callback
    def shutdown(self) -> None:
        """Cancel pending timers."""
        self._cancel("_ring_timer")
        self._cancel("_call_timer")
