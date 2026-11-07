"""Base entity for the Ajax DoorBell integration."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from .doorbell import AjaxDoorbell


class AjaxDoorbellEntity(Entity):
    """An entity attached to one DoorBell device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, doorbell: AjaxDoorbell, key: str) -> None:
        """Initialize the entity."""
        self._doorbell = doorbell
        self._attr_unique_id = f"{doorbell.unique_base}_{key}"
        self._attr_device_info = doorbell.device_info
