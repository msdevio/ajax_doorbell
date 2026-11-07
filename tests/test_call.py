"""Tests for the call state machine."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ajax_doorbell.call import CallManager, CallState
from custom_components.ajax_doorbell.const import EVENT_CALL
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


def _manager(hass: HomeAssistant) -> CallManager:
    return CallManager(hass, lambda: {"device_id": "dev"}, ring_duration=30, call_timeout=300)


async def _later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_unanswered_ring_is_missed(hass: HomeAssistant) -> None:
    """A ring nobody answers becomes a missed call."""
    events = async_capture_events(hass, EVENT_CALL)
    call = _manager(hass)
    call.ring()
    assert call.state is CallState.RINGING
    assert call.ring_started is not None
    await _later(hass, 31)
    assert call.state is CallState.IDLE
    assert call.missed_calls == 1
    assert call.last_missed is not None
    assert [e.data["type"] for e in events] == ["ring", "missed"]
    assert events[0].data["device_id"] == "dev"
    call.clear_missed()
    assert call.missed_calls == 0


async def test_answer_then_hang_up(hass: HomeAssistant) -> None:
    """Answering stops the ringing; answering again only keeps the call alive."""
    events = async_capture_events(hass, EVENT_CALL)
    call = _manager(hass)
    call.ring()
    call.answer("Tablette")
    assert call.state is CallState.IN_CALL
    assert call.answered_by == "Tablette"
    call.answer("Tablette")
    await _later(hass, 31)
    assert call.missed_calls == 0
    call.hang_up()
    assert call.state is CallState.IDLE
    await hass.async_block_till_done()
    assert [e.data["type"] for e in events] == ["ring", "answered", "ended"]
    assert events[1].data["after_ring"] is True


async def test_call_safety_timeout(hass: HomeAssistant) -> None:
    """A call nobody hangs up ends by itself."""
    events = async_capture_events(hass, EVENT_CALL)
    call = _manager(hass)
    call.answer()
    assert call.ring_started is None
    assert call.answered_by == "Home Assistant"
    await _later(hass, 301)
    assert call.state is CallState.IDLE
    assert events[-1].data == {
        "type": "ended",
        "state": CallState.IDLE,
        "device_id": "dev",
        "reason": "timeout",
    }


async def test_dismiss(hass: HomeAssistant) -> None:
    """Ignoring a ring is not a missed call."""
    call = _manager(hass)
    call.ring()
    call.dismiss()
    await _later(hass, 31)
    assert call.state is CallState.IDLE
    assert call.missed_calls == 0


async def test_hang_up_while_ringing_declines(hass: HomeAssistant) -> None:
    """Hanging up a ringing call declines it rather than leaving it to be missed."""
    events = async_capture_events(hass, EVENT_CALL)
    call = _manager(hass)
    call.ring()
    call.hang_up()
    await _later(hass, 31)
    assert call.state is CallState.IDLE
    assert call.missed_calls == 0
    assert [e.data["type"] for e in events] == ["ring", "dismissed"]


async def test_ring_during_call(hass: HomeAssistant) -> None:
    """A second press during a call is reported but changes nothing."""
    events = async_capture_events(hass, EVENT_CALL)
    call = _manager(hass)
    call.answer("Téléphone")
    call.ring()
    await hass.async_block_till_done()
    assert call.state is CallState.IN_CALL
    assert events[-1].data["type"] == "ring"
    assert events[-1].data["during_call"] is True
    call.shutdown()


async def test_restore(hass: HomeAssistant) -> None:
    """Restored values do not overwrite live ones."""
    call = _manager(hass)
    before = dt_util.utcnow() - timedelta(hours=1)
    call.restore(missed_calls=2, last_ring=before, last_missed=before)
    assert (call.missed_calls, call.last_ring) == (2, before)
    call.ring()
    call.restore(last_ring=before)
    assert call.last_ring != before
    call.shutdown()
