"""Run the notification blueprint as Home Assistant does, with a fake phone."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

BLUEPRINT = (
    Path(__file__).parent.parent
    / "blueprints"
    / "automation"
    / "ajax_doorbell"
    / "doorbell_notify.yaml"
)
NOTIFIED = "Quelqu'un est devant la porte."
WAITING = 3  # the blueprint waits for three ajax_doorbell_call events


async def _until(condition: Callable[[], bool]) -> None:
    """Let the automation run (block_till_done would sit out its 5 minute wait)."""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the automation never got there")


async def _setup(hass: HomeAssistant, **inputs: Any) -> SimpleNamespace:
    """A phone renamed by its owner, the DoorBell's entities and the automation."""
    target = Path(hass.config.path("blueprints/automation/ajax_doorbell"))

    def install() -> None:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy(BLUEPRINT, target / "doorbell_notify.yaml")

    await hass.async_add_executor_job(install)

    phone_entry = MockConfigEntry(domain="mobile_app")
    phone_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    phone = devices.async_get_or_create(
        config_entry_id=phone_entry.entry_id,
        identifiers={("mobile_app", "phone")},
        manufacturer="samsung",
        model="SM-S908B",
        name="SM-S908B",
    )
    devices.async_update_device(phone.id, name_by_user="M-SAÏD")
    bell_entry = MockConfigEntry(domain="ajax_doorbell")
    bell_entry.add_to_hass(hass)
    bell = devices.async_get_or_create(
        config_entry_id=bell_entry.entry_id,
        identifiers={("ajax_doorbell", "bell")},
        name="Ajax DoorBell",
    )
    entities = er.async_get(hass)
    for domain, key in (("event", "ring"), ("sensor", "call_state"), ("camera", "camera")):
        entities.async_get_or_create(
            domain,
            "ajax_doorbell",
            key,
            device_id=bell.id,
            suggested_object_id=f"porte_{key}",
            config_entry=bell_entry,
        )
    hass.states.async_set("event.porte_ring", "unknown", {"event_types": ["ring"]})
    hass.states.async_set("sensor.porte_call_state", "idle")
    hass.states.async_set("camera.porte_camera", "idle")
    world = SimpleNamespace(
        bell=bell.id,
        sent=async_mock_service(hass, "notify", "mobile_app_sm_s908b"),
        dismissed=async_mock_service(hass, "ajax_doorbell", "dismiss"),
        opened=async_mock_service(hass, "lock", "open"),
    )
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "doorbell",
                "use_blueprint": {
                    "path": "ajax_doorbell/doorbell_notify.yaml",
                    "input": {
                        "ring_event": "event.porte_ring",
                        "call_sensor": "sensor.porte_call_state",
                        "camera": "camera.porte_camera",
                        "notify_devices": [phone.id],
                        "dashboard": "/lovelace/0",
                        **inputs,
                    },
                },
            }
        },
    )
    await hass.async_block_till_done()
    return world


def _waiting(hass: HomeAssistant) -> int:
    return hass.bus.async_listeners().get("ajax_doorbell_call", 0)


async def _ring(hass: HomeAssistant, world: SimpleNamespace) -> dict[str, Any]:
    """Ring, and return the notification once the automation waits for an answer."""
    hass.states.async_set(
        "event.porte_ring",
        "2026-11-07T19:19:44.000+00:00",
        {"event_type": "ring", "event_types": ["ring"]},
    )
    await _until(lambda: _waiting(hass) == WAITING)
    return world.sent[0].data["data"]


async def _then(hass: HomeAssistant, event_type: str, data: dict[str, Any]) -> None:
    """Fire what ends the call; the automation finishes its run."""
    hass.bus.async_fire(event_type, data)
    await _until(lambda: _waiting(hass) == 0)
    await hass.async_block_till_done()


def _messages(world: SimpleNamespace) -> list[str]:
    return [call.data["message"] for call in world.sent]


async def test_ring_notifies_the_phone(hass: HomeAssistant) -> None:
    """The notification (its variables render) and the view opened on Android."""
    world = await _setup(hass, open_on_ring=True)
    notification = await _ring(hass, world)
    assert _messages(world) == [NOTIFIED, "command_webview"]
    assert notification["clickAction"] == "/lovelace/0?ajax_answer=camera.porte_camera"
    assert notification["image"] == "/api/camera_proxy/camera.porte_camera"
    assert notification["tag"] == "ajax_doorbell_porte_camera"
    assert [button["title"] for button in notification["actions"]] == [
        "Décrocher",
        "Voir",
        "Ignorer",
    ]
    assert world.sent[1].data["data"]["command"] == "/lovelace/0"
    await _then(hass, "ajax_doorbell_call", {"device_id": world.bell, "type": "answered"})


@pytest.mark.parametrize("outcome", ["answered", "dismissed", "missed"])
async def test_call_ends(hass: HomeAssistant, outcome: str) -> None:
    """The notification goes away; a missed call leaves one behind."""
    world = await _setup(hass)
    await _ring(hass, world)
    hass.bus.async_fire("ajax_doorbell_call", {"device_id": "another bell", "type": outcome})
    await asyncio.sleep(0.2)  # block_till_done would wait for the whole run
    assert _waiting(hass) == WAITING, "another DoorBell's call"
    await _then(hass, "ajax_doorbell_call", {"device_id": world.bell, "type": outcome})
    assert _messages(world)[:2] == [NOTIFIED, "clear_notification"]
    assert world.sent[1].data["data"] == {"tag": "ajax_doorbell_porte_camera"}
    if outcome == "missed":
        assert len(world.sent) == 3
        assert world.sent[2].data["title"] == "Appel manqué"
        assert world.sent[2].data["data"]["tag"] == "ajax_doorbell_porte_camera_missed"
    else:
        assert len(world.sent) == 2
    assert not world.dismissed


async def test_ignore_button(hass: HomeAssistant) -> None:
    """Ignorer ends the call on the DoorBell; a button from an older ring does nothing."""
    world = await _setup(hass)
    notification = await _ring(hass, world)
    ignore = notification["actions"][2]["action"]
    assert ignore.startswith("AJAX_DOORBELL_DISMISS_")
    hass.bus.async_fire("mobile_app_notification_action", {"action": "AJAX_DOORBELL_DISMISS_1"})
    await asyncio.sleep(0.2)  # block_till_done would wait for the whole run
    assert _waiting(hass) == WAITING
    await _then(hass, "mobile_app_notification_action", {"action": ignore})
    assert [call.data["entity_id"] for call in world.dismissed] == [["sensor.porte_call_state"]]
    assert _messages(world) == [NOTIFIED, "clear_notification"]


async def test_open_button(hass: HomeAssistant) -> None:
    """With a lock chosen, « Ouvrir la porte » opens it."""
    world = await _setup(hass, lock="lock.porte")
    notification = await _ring(hass, world)
    button = notification["actions"][3]
    assert button["title"] == "Ouvrir la porte"
    assert button["authenticationRequired"] is True
    await _then(hass, "mobile_app_notification_action", {"action": button["action"]})
    assert [call.data["entity_id"] for call in world.opened] == [["lock.porte"]]
    assert not world.dismissed
    assert _messages(world) == [NOTIFIED, "clear_notification"]
