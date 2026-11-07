"""Tests for the PullPoint loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ajax_doorbell.const import DOMAIN
from custom_components.ajax_doorbell.events import EventStream
from custom_components.ajax_doorbell.onvif import (
    OnvifAuthError,
    PullPoint,
    parse_notifications,
)
from homeassistant.core import HomeAssistant

from .conftest import HOST, load_xml


def _client() -> MagicMock:
    client = MagicMock()
    client.host = HOST
    client.async_create_pullpoint = AsyncMock(return_value=PullPoint("http://x/sub"))
    client.async_set_synchronization_point = AsyncMock()
    client.async_renew = AsyncMock()
    client.async_unsubscribe = AsyncMock()
    client.async_update_time_offset = AsyncMock()
    return client


async def test_dispatches_and_releases(hass: HomeAssistant) -> None:
    """Events are handed over; stopping releases the subscription."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    events = parse_notifications(load_xml("pull_messages_ring.xml"))
    client = _client()
    gate = asyncio.Event()
    batches = [events]

    async def pull(pullpoint: PullPoint, wait: int) -> list:
        if batches:
            return batches.pop()
        await gate.wait()
        return []

    client.async_pull = pull
    received, connection, auth = [], [], []
    stream = EventStream(hass, client, received.append, connection.append, lambda: auth.append(1))
    stream.start(entry)
    for _ in range(5):
        await asyncio.sleep(0)
    assert received == events
    assert connection == [True]
    assert stream.connected
    await stream.async_stop()
    client.async_unsubscribe.assert_awaited_once()
    assert auth == []


async def test_auth_error_after_clock_resync(hass: HomeAssistant) -> None:
    """A refused account is retried once after reading the clock, then reported."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = _client()
    client.async_create_pullpoint.side_effect = OnvifAuthError("no")
    auth: list[int] = []
    stream = EventStream(hass, client, lambda e: None, lambda c: None, lambda: auth.append(1))
    stream.start(entry)
    for _ in range(10):
        await asyncio.sleep(0)
    assert auth == [1]
    client.async_update_time_offset.assert_awaited_once()
    assert client.async_create_pullpoint.await_count == 2
    await stream.async_stop()
