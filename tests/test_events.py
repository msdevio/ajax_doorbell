"""Tests for the PullPoint loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ajax_doorbell import events
from custom_components.ajax_doorbell.const import DOMAIN
from custom_components.ajax_doorbell.events import EventStream
from custom_components.ajax_doorbell.onvif import (
    OnvifAuthError,
    OnvifConnectionError,
    OnvifError,
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


class Recorder:
    """Collects what the stream reports."""

    def __init__(self) -> None:
        self.events: list = []
        self.connection: list[bool] = []
        self.refused = AsyncMock()
        self.recovered: list[int] = []

    def stream(self, hass: HomeAssistant, client: MagicMock) -> EventStream:
        return EventStream(
            hass,
            client,
            self.events.append,
            self.connection.append,
            self.refused,
            lambda: self.recovered.append(1),
        )


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry to own the background task."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """No waiting between attempts."""
    monkeypatch.setattr(events, "RESYNC_DELAY", 0)
    monkeypatch.setattr(events, "REFUSED_DELAY", 0)


async def _spin(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def test_dispatches_and_releases(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Events are handed over; stopping releases the subscription."""
    batch = parse_notifications(load_xml("pull_messages_ring.xml"))
    client = _client()
    gate = asyncio.Event()
    batches = [batch]

    async def pull(pullpoint: PullPoint, wait: int) -> list:
        if batches:
            return batches.pop()
        await gate.wait()
        return []

    client.async_pull = pull
    recorder = Recorder()
    stream = recorder.stream(hass, client)
    stream.start(entry)
    await _spin()
    assert recorder.events == batch
    assert recorder.connection == [True]
    await stream.async_stop()
    client.async_unsubscribe.assert_awaited_once()
    assert recorder.connection == [True, False]
    recorder.refused.assert_not_awaited()


async def test_connected_only_after_a_pull(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A subscription whose pulls fail never reports the stream as up."""
    client = _client()
    client.async_pull = AsyncMock(side_effect=OnvifConnectionError("blocked"))
    recorder = Recorder()
    stream = recorder.stream(hass, client)
    stream.start(entry)
    await _spin()
    assert recorder.connection == []
    assert stream.last_error == "blocked"
    await stream.async_stop()


@pytest.mark.usefixtures("fast")
async def test_refusal_is_reported_once_and_retried(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A refusal resyncs the clock, then is reported once while retries go on."""
    client = _client()
    client.async_create_pullpoint.side_effect = OnvifAuthError("no")
    recorder = Recorder()
    stream = recorder.stream(hass, client)
    stream.start(entry)
    await _spin(60)
    recorder.refused.assert_awaited_once()
    client.async_update_time_offset.assert_awaited()
    assert client.async_create_pullpoint.await_count > 3
    assert stream.connected is False

    # The device accepts the account again (it had rebooted): events resume.
    client.async_create_pullpoint.side_effect = None
    gate = asyncio.Event()

    async def pull(pullpoint: PullPoint, wait: int) -> list:
        await asyncio.sleep(0)
        if not stream.connected:
            return []
        await gate.wait()
        return []

    client.async_pull = pull
    await _spin(60)
    assert stream.connected is True
    assert recorder.recovered == [1]
    await stream.async_stop()


async def test_refused_filter_falls_back(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A device that refuses the topic filter gets a plain subscription, for good."""
    client = _client()
    client.async_create_pullpoint.side_effect = [
        OnvifError("InvalidFilterFault"),
        PullPoint("http://x/sub"),
    ]
    gate = asyncio.Event()

    async def pull(pullpoint: PullPoint, wait: int) -> list:
        await gate.wait()
        return []

    client.async_pull = pull
    recorder = Recorder()
    stream = recorder.stream(hass, client)
    stream.topics = ["tns1:RuleEngine/tnsajax:RingDetector/Detection"]
    stream.start(entry)
    await _spin()
    calls = client.async_create_pullpoint.await_args_list
    assert [call.args[1:] for call in calls] == [(stream.topics,), ()]
    assert stream.filtered is False
    await stream.async_stop()


async def test_silent_filter_is_dropped(
    hass: HomeAssistant, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing at all through the filter means it filters too much."""
    monkeypatch.setattr(events, "FILTER_SILENCE", 0)
    client = _client()
    batch = parse_notifications(load_xml("pull_messages_ajax.xml"))
    gate = asyncio.Event()
    pulls: list[bool] = []

    async def pull(pullpoint: PullPoint, wait: int) -> list:
        pulls.append(stream.filtered)
        if stream.filtered:
            return []
        if len(pulls) == 2:
            return batch
        await gate.wait()
        return []

    client.async_pull = pull
    recorder = Recorder()
    stream = recorder.stream(hass, client)
    stream.topics = ["tns1:RuleEngine/tnsajax:RingDetector/Detection"]
    stream.start(entry)
    await _spin(60)
    assert pulls[:2] == [True, False]
    assert recorder.events == batch
    assert [call.args[1:] for call in client.async_create_pullpoint.await_args_list] == [
        (stream.topics,),
        (),
    ]
    await stream.async_stop()
