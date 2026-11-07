"""Tests for setup, entities, ring handling and services."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ajax_doorbell.const import DOMAIN, EVENT_CALL
from custom_components.ajax_doorbell.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ajax_doorbell.onvif import (
    OnvifAuthError,
    OnvifConnectionError,
    OnvifEvent,
    parse_notifications,
)
from custom_components.ajax_doorbell.rtsp import StreamDescription
from homeassistant.components.camera.helper import get_camera_from_entity_id
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import MAC, load_xml

pytestmark = pytest.mark.usefixtures("mock_client", "no_event_stream")

try:
    from homeassistant.components.camera import CameraEntityCapabilities  # noqa: F401
except ImportError:  # Home Assistant 2026.10
    HAS_TWO_WAY_AUDIO = False
else:
    HAS_TWO_WAY_AUDIO = True


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{MAC}_{key}")
    assert entity_id, f"{platform} {key} missing"
    return entity_id


@pytest.mark.usefixtures("mock_describe")
async def test_entities(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Setup creates the cameras, the ring event and the call sensors."""
    await _setup(hass, mock_entry)
    assert mock_entry.state is ConfigEntryState.LOADED

    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, MAC), mock_entry.entry_id)
    assert (device.manufacturer, device.model, device.model_id) == (
        "Ajax Systems",
        "DoorBell",
        "oa8000-nand",
    )
    assert device.sw_version == "2.669-1"
    assert (dr.CONNECTION_NETWORK_MAC, MAC) in device.connections

    camera = get_camera_from_entity_id(hass, _entity(hass, "camera", "camera"))
    if HAS_TWO_WAY_AUDIO:
        assert camera.camera_entity_capabilities.two_way_audio is True
    assert mock_entry.runtime_data.two_way_audio is True
    assert await camera.stream_source() == (
        "rtsp://homeassistant:secret@192.168.128.31:8554/9c756e123456-0_m"
    )
    sub = er.async_get(hass).async_get(_entity(hass, "camera", "camera_sub"))
    assert sub.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    assert hass.states.get(_entity(hass, "sensor", "call_state")).state == "idle"
    assert hass.states.get(_entity(hass, "binary_sensor", "ringing")).state == "off"
    assert hass.states.get(_entity(hass, "binary_sensor", "event_stream")).state == "on"
    for key in ("motion", "person", "vehicle", "pet"):
        assert hass.states.get(_entity(hass, "binary_sensor", key)).state == "off"
    assert hass.states.get(_entity(hass, "event", "ring")).state == "unknown"


async def test_no_backchannel(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Without a usable backchannel the camera does not offer talk-back."""
    with patch(
        "custom_components.ajax_doorbell.doorbell.async_describe",
        AsyncMock(return_value=StreamDescription(551, ())),
    ):
        await _setup(hass, mock_entry)
    camera = get_camera_from_entity_id(hass, _entity(hass, "camera", "camera"))
    if HAS_TWO_WAY_AUDIO:
        assert camera.camera_entity_capabilities.two_way_audio is False
    assert mock_entry.runtime_data.two_way_audio is False


@pytest.mark.usefixtures("mock_describe")
async def test_ring_answer_hang_up(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """A ring sets everything ringing; the services answer and hang up."""
    await _setup(hass, mock_entry)
    doorbell = mock_entry.runtime_data
    calls = async_capture_events(hass, EVENT_CALL)
    call_state = _entity(hass, "sensor", "call_state")

    for event in parse_notifications(load_xml("pull_messages_ring.xml")):
        doorbell._on_event(event)
    await hass.async_block_till_done()

    assert hass.states.get(_entity(hass, "event", "ring")).attributes["event_type"] == "ring"
    assert hass.states.get(call_state).state == "ringing"
    assert hass.states.get(_entity(hass, "binary_sensor", "ringing")).state == "on"
    assert hass.states.get(_entity(hass, "binary_sensor", "person")).state == "on"
    assert hass.states.get(_entity(hass, "binary_sensor", "pet")).state == "on"
    assert hass.states.get(_entity(hass, "binary_sensor", "vehicle")).state == "off"
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, MAC), mock_entry.entry_id)
    assert calls[0].data["type"] == "ring"
    assert calls[0].data["device_id"] == device.id

    await hass.services.async_call(
        DOMAIN, "answer", {"entity_id": call_state, "answered_by": "Tablette"}, blocking=True
    )
    state = hass.states.get(call_state)
    assert state.state == "in_call"
    assert state.attributes["answered_by"] == "Tablette"
    assert hass.states.get(_entity(hass, "binary_sensor", "ringing")).state == "off"

    await hass.services.async_call(DOMAIN, "hang_up", {"device_id": device.id}, blocking=True)
    assert hass.states.get(call_state).state == "idle"
    assert [c.data["type"] for c in calls] == ["ring", "answered", "ended"]


@pytest.mark.usefixtures("mock_describe")
async def test_ring_filters(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Replayed values and double messages are not new presses."""
    await _setup(hass, mock_entry)
    doorbell = mock_entry.runtime_data
    calls = async_capture_events(hass, EVENT_CALL)

    def ring(operation: str, value: str = "true") -> OnvifEvent:
        return OnvifEvent(
            "tns1:RuleEngine/RingDetector/Detection", operation, None, {}, {"Detected": value}
        )

    doorbell._on_event(ring("Initialized"))
    doorbell._on_event(ring("Changed", "false"))
    doorbell._on_event(ring("Changed"))
    doorbell._on_event(ring("Changed"))
    await hass.async_block_till_done()
    assert [c.data["type"] for c in calls] == ["ring"]


@pytest.mark.usefixtures("mock_describe")
async def test_services_without_target(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """No target means every DoorBell; a foreign target is refused."""
    await _setup(hass, mock_entry)
    mock_entry.runtime_data.call.ring()
    await hass.services.async_call(DOMAIN, "dismiss", {}, blocking=True)
    assert hass.states.get(_entity(hass, "sensor", "call_state")).state == "idle"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "answer", {"entity_id": "light.kitchen"}, blocking=True
        )


async def test_setup_auth_error(
    hass: HomeAssistant, mock_client: MagicMock, mock_entry: MockConfigEntry
) -> None:
    """A refused account starts a reauth flow."""
    mock_client.async_get_identity.side_effect = OnvifAuthError("no")
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_setup_not_ready(
    hass: HomeAssistant, mock_client: MagicMock, mock_entry: MockConfigEntry
) -> None:
    """An unreachable DoorBell is retried."""
    mock_client.async_update_time_offset.side_effect = OnvifConnectionError("down")
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.usefixtures("mock_describe")
async def test_unload_and_diagnostics(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Diagnostics hide secrets; unloading stops everything."""
    await _setup(hass, mock_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_entry)
    assert diagnostics["entry"]["data"]["password"] == "**REDACTED**"
    assert diagnostics["device"]["serial"] == "**REDACTED**"
    assert diagnostics["stream_uris"]["main_token"] == (
        "rtsp://192.168.128.31:8554/9c756e123456-0_m"
    )
    assert diagnostics["two_way_audio"] is True
    assert "secret" not in str(diagnostics)
    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    assert mock_entry.state is ConfigEntryState.NOT_LOADED
