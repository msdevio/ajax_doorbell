"""The notification blueprint is valid for Home Assistant."""

from __future__ import annotations

from pathlib import Path

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    async_validate_config_item,
)
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.template import Template
from homeassistant.util.yaml import load_yaml_dict

BLUEPRINT = (
    Path(__file__).parent.parent
    / "blueprints"
    / "automation"
    / "ajax_doorbell"
    / "doorbell_notify.yaml"
)


async def test_blueprint_validates(hass: HomeAssistant) -> None:
    """The blueprint, with its inputs filled, is a valid automation."""
    blueprint = Blueprint(
        load_yaml_dict(BLUEPRINT),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    inputs = BlueprintInputs(
        blueprint,
        {
            "use_blueprint": {
                "path": "ajax_doorbell/doorbell_notify.yaml",
                "input": {
                    "ring_event": "event.ajax_doorbell_doorbell",
                    "call_sensor": "sensor.ajax_doorbell_call",
                    "camera": "camera.ajax_doorbell",
                    "notify_devices": ["0123456789abcdef"],
                    "lock": "lock.porte_d_entree",
                },
            }
        },
    )
    inputs.validate()
    config = inputs.async_substitute()
    assert await async_validate_config_item(hass, "doorbell", {**config, "id": "doorbell"})


async def test_notify_service_of_a_renamed_phone(hass: HomeAssistant) -> None:
    """A phone renamed in Home Assistant keeps the notify service of its own name."""
    entry = MockConfigEntry(domain="mobile_app")
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    phone = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("mobile_app", "phone")},
        manufacturer="samsung",
        model="SM-S908B",
        name="SM-S908B",
    )
    registry.async_update_device(phone.id, name_by_user="M-SAÏD")
    variables = load_yaml_dict(BLUEPRINT)["variables"]
    for key in ("notify_services", "android_services"):
        rendered = Template(variables[key], hass).async_render({"phones": [phone.id]})
        assert rendered == ["notify.mobile_app_sm_s908b"]
