"""Tests for setup, entities, ring handling and services."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
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
from custom_components.ajax_doorbell.rtsp import RtspError, StreamDescription
from homeassistant.components.camera.helper import get_camera_from_entity_id
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.util import dt as dt_util

from .conftest import DESCRIPTION, MAC, load_xml

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
        "rtsp://192.168.128.31:8554/**REDACTED**-0_m"
    )
    assert "9c756e123456" not in str(diagnostics)
    assert diagnostics["two_way_audio"] is True
    assert "secret" not in str(diagnostics)
    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    assert mock_entry.state is ConfigEntryState.NOT_LOADED


async def test_talk_back_learnt_after_setup(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """A stream probe that failed at setup is retried once events flow."""
    with patch(
        "custom_components.ajax_doorbell.doorbell.async_describe",
        AsyncMock(side_effect=[RtspError("busy"), DESCRIPTION]),
    ):
        await _setup(hass, mock_entry)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_entry.runtime_data.two_way_audio is True
    if HAS_TWO_WAY_AUDIO:
        camera = get_camera_from_entity_id(hass, _entity(hass, "camera", "camera"))
        assert camera.camera_entity_capabilities.two_way_audio is True


@pytest.mark.usefixtures("mock_describe")
async def test_events_refused(
    hass: HomeAssistant, mock_client: MagicMock, mock_entry: MockConfigEntry
) -> None:
    """A role without events raises a repair; a wrong password asks for reauth."""
    await _setup(hass, mock_entry)
    doorbell = mock_entry.runtime_data
    issue_id = f"event_permission_{mock_entry.entry_id}"

    await doorbell._async_events_refused()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    doorbell._events_recovered()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    mock_client.async_get_profiles.side_effect = OnvifAuthError("no")
    await doorbell._async_events_refused()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


@pytest.mark.usefixtures("mock_describe")
async def test_motion_follows_the_device(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Motion stays on while the device says so; an AI detection never cleared times out."""
    await _setup(hass, mock_entry)
    doorbell = mock_entry.runtime_data

    def motion(value: str) -> OnvifEvent:
        return OnvifEvent(
            "tns1:RuleEngine/tnsajax:MotionDetector/Detection", "", None, {}, {"State": value}
        )

    doorbell._on_event(motion("true"))
    doorbell._on_event(
        OnvifEvent(
            "tns1:RuleEngine/tnsajax:ObjectDetector/Detection",
            "Changed",
            None,
            {},
            {"ClassTypes": "Human"},
        )
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "binary_sensor", "motion")).state == "on"
    assert hass.states.get(_entity(hass, "binary_sensor", "person")).state == "on"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "binary_sensor", "person")).state == "off"
    doorbell._on_event(motion("false"))
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "binary_sensor", "motion")).state == "off"


@pytest.mark.usefixtures("mock_describe")
async def test_ajax_messages(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Real DoorBell traffic: PIR, tracking boxes and repeated states do not mislead."""
    await _setup(hass, mock_entry)
    doorbell = mock_entry.runtime_data
    calls = async_capture_events(hass, EVENT_CALL)
    for event in parse_notifications(load_xml("pull_messages_ajax.xml")):
        doorbell._on_event(event)
    await hass.async_block_till_done()
    # The tracking message (no ClassTypes) does not clear the person.
    assert hass.states.get(_entity(hass, "binary_sensor", "person")).state == "on"
    # PIR turning off does not clear the camera's motion, and has its own sensor.
    assert hass.states.get(_entity(hass, "binary_sensor", "motion")).state == "on"
    assert hass.states.get(_entity(hass, "binary_sensor", "pir")).state == "off"
    # "Detected" repeated while the button is held is one press.
    assert [c.data["type"] for c in calls] == ["ring"]
    assert hass.states.get(_entity(hass, "sensor", "call_state")).state == "ringing"
    assert [e["topic"].rsplit("/", 2)[-2] for e in doorbell.last_events] == [
        "ObjectDetection",
        "tnsajax:PirMotionDetector",
        "tnsajax:MotionDetector",
        "tnsajax:RingDetector",
        "tnsajax:PirMotionDetector",
    ]
    doorbell.call.shutdown()


@pytest.mark.usefixtures("mock_describe")
async def test_detections_combine_rules(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """A detection is on while any rule reports it."""
    await _setup(hass, mock_entry)
    doorbell = mock_entry.runtime_data

    def objects(rule: str, classes: str) -> OnvifEvent:
        return OnvifEvent(
            "tns1:RuleEngine/ObjectDetection/Object",
            "Changed",
            None,
            {"Rule": rule},
            {"ClassTypes": classes},
        )

    person = _entity(hass, "binary_sensor", "person")
    doorbell._on_event(objects("r1", "Human"))
    doorbell._on_event(objects("r2", ""))
    await hass.async_block_till_done()
    assert hass.states.get(person).state == "on"
    doorbell._on_event(objects("r1", ""))
    await hass.async_block_till_done()
    assert hass.states.get(person).state == "off"


@pytest.mark.usefixtures("mock_describe")
async def test_subscription_topics(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """The subscription asks for the used topics only, without the tracking flood."""
    await _setup(hass, mock_entry)
    assert mock_entry.runtime_data.events.topics == [
        "tns1:RuleEngine/tnsajax:RingDetector/Detection",
        "tns1:RuleEngine/tnsajax:PirMotionDetector/Detection",
        "tns1:RuleEngine/ObjectDetection/Object",
        "tns1:RuleEngine/tnsajax:MotionDetector/Detection",
        "tns1:VideoSource/MotionAlarm",
    ]


@pytest.mark.usefixtures("mock_describe")
async def test_prefixed_ring_topic(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Namespace prefixes on any segment do not hide a ring."""
    await _setup(hass, mock_entry)
    mock_entry.runtime_data._on_event(
        OnvifEvent(
            "tns1:RuleEngine/tnsajax:RingDetector/tnsajax:Detection",
            "Changed",
            None,
            {},
            {"Detected": "true"},
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "sensor", "call_state")).state == "ringing"
    mock_entry.runtime_data.call.shutdown()


async def test_card_is_served(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """With a web server, the card is served and loaded on every dashboard."""
    hass.config.components.add("frontend")
    with (
        patch(
            "homeassistant.components.http.HomeAssistantHTTP.async_register_static_paths",
            AsyncMock(),
        ) as register,
        patch("custom_components.ajax_doorbell.add_extra_js_url") as add_url,
        patch(
            "custom_components.ajax_doorbell.doorbell.async_describe",
            AsyncMock(return_value=DESCRIPTION),
        ),
    ):
        await _setup(hass, mock_entry)
    (paths,) = register.await_args.args
    assert paths[0].url_path == "/ajax_doorbell/ajax-doorbell-card.js"
    assert paths[0].path.endswith("frontend/ajax-doorbell-card.js")
    url = add_url.call_args.args[1]
    assert url.startswith("/ajax_doorbell/ajax-doorbell-card.js?v=")
    assert len(url.rsplit("=", 1)[1]) == 10
