"""Tests for the visit log and what the card uses to replay visits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.ajax_doorbell.const import DOMAIN
from custom_components.ajax_doorbell.onvif import OnvifEvent
from custom_components.ajax_doorbell.visits import VisitLog
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import ENTRY_DATA, MAC

T0 = datetime(2026, 11, 7, 11, 0, 0, tzinfo=UTC)


async def test_visits_merge_and_expire(hass: HomeAssistant, hass_storage: dict) -> None:
    """A person then a ring within a minute is one visit; old visits go."""
    log = VisitLog(hass, "e1", retention_days=2)
    await log.async_load()
    log.add("person", T0)
    log.add("ring", T0 + timedelta(seconds=40))
    log.add("person", T0 + timedelta(minutes=10))
    assert [(v.kind, v.start) for v in log.visits] == [
        ("ring", T0),
        ("person", T0 + timedelta(minutes=10)),
    ]
    assert log.visits[0].end == T0 + timedelta(seconds=40)
    assert log.get(T0.strftime("%Y%m%dT%H%M%SZ")) is log.visits[0]
    log.add("ring", T0 + timedelta(days=2, minutes=5))
    assert [v.start for v in log.visits] == [
        T0 + timedelta(minutes=10),
        T0 + timedelta(days=2, minutes=5),
    ]

    # Saved (delayed: flushed here as on shutdown), and read back.
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    assert len(hass_storage[f"{DOMAIN}.e1.visits"]["data"]) == 2
    again = VisitLog(hass, "e1", retention_days=30)
    await again.async_load()
    assert [v.kind for v in again.visits] == ["person", "ring"]


def _nvr() -> MagicMock:
    nvr = MagicMock()
    nvr.replay_url = lambda start, quality="main": (
        f"rtsp://127.0.0.1:9/replay/{quality}/{start:%H%M%S}"
    )
    nvr.async_thumbnail = AsyncMock(return_value=b"\xff\xd8jpeg")
    nvr.async_close = AsyncMock()
    return nvr


@pytest.fixture
def nvr_entry() -> MockConfigEntry:
    """A DoorBell entry with an NVR in its options."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Ajax DoorBell",
        unique_id=MAC,
        data=dict(ENTRY_DATA),
        options={"nvr_host": "192.168.128.30"},
    )


@pytest.mark.usefixtures("mock_client", "no_event_stream", "mock_describe")
async def test_visits_and_replay(
    hass: HomeAssistant, nvr_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """The card lists visits with thumbnails, then replays one through the replay camera."""
    nvr = _nvr()
    with patch(
        "custom_components.ajax_doorbell.doorbell.AjaxDoorbell._async_connect_nvr", AsyncMock()
    ):
        nvr_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(nvr_entry.entry_id)
        await hass.async_block_till_done()
    doorbell = nvr_entry.runtime_data
    camera = er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"{MAC}_camera")
    replay = er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"{MAC}_replay")
    assert replay and er.async_get(hass).async_get(replay).hidden_by is not None
    assert hass.states.get(replay).state == "unavailable"  # NVR not reached yet

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/visits", "entity_id": camera})
    assert (await client.receive_json())["success"]
    first = (await client.receive_json())["event"]
    assert first["visits"] == [] and first["nvr"] is False and first["nvr_configured"]

    replay_camera = hass.data["camera"].get_entity(replay)
    with patch.object(type(replay_camera), "async_refresh_providers", AsyncMock()) as refresh:
        doorbell.nvr = nvr
        doorbell._notify_replay()
        await client.receive_json()  # the NVR is there now
        await hass.async_block_till_done()
    refresh.assert_awaited()  # WebRTC provider picked again, now that there is a source
    doorbell._on_event(
        OnvifEvent(
            "tns1:RuleEngine/ObjectDetection/Object", "Changed", None, {}, {"ClassTypes": "Human"}
        )
    )
    doorbell._on_event(
        OnvifEvent(
            "tns1:RuleEngine/tnsajax:RingDetector/Detection", "", None, {}, {"Detected": "true"}
        )
    )
    event = (await client.receive_json())["event"]
    while len(event["visits"]) != 1 or event["visits"][0]["kind"] != "ring":
        event = (await client.receive_json())["event"]
    visit = event["visits"][0]
    assert event["replay_camera"] == replay
    assert visit["thumb"].startswith(
        f"/api/{DOMAIN}/thumb/{nvr_entry.entry_id}/{visit['id']}?authSig="
    )

    start = (dt_util.parse_datetime(visit["start"]) - timedelta(seconds=5)).isoformat()
    await client.send_json(
        {"id": 2, "type": f"{DOMAIN}/replay", "entity_id": camera, "start": start}
    )
    result = await client.receive_json()
    while result.get("id") != 2:
        result = await client.receive_json()
    assert result["result"] == {"camera": replay}
    await hass.async_block_till_done()
    assert await replay_camera.stream_source() == nvr.replay_url(dt_util.parse_datetime(start))
    assert hass.states.get(replay).attributes["replay_start"] == start
    doorbell.call.shutdown()


@pytest.mark.usefixtures("mock_client", "no_event_stream", "mock_describe")
async def test_replay_without_nvr(
    hass: HomeAssistant, nvr_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """Replaying before the NVR answers is an error, not an exception."""
    with patch(
        "custom_components.ajax_doorbell.doorbell.AjaxDoorbell._async_connect_nvr", AsyncMock()
    ):
        nvr_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(nvr_entry.entry_id)
        await hass.async_block_till_done()
    camera = er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"{MAC}_camera")
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": f"{DOMAIN}/replay", "entity_id": camera, "start": T0.isoformat()}
    )
    assert (await client.receive_json())["error"]["code"] == "nvr_unavailable"
