"""The notification blueprint is valid for Home Assistant."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    async_validate_config_item,
)
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.core import HomeAssistant
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
