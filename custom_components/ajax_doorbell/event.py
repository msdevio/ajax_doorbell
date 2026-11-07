"""Doorbell event entity: the standard trigger for 'someone rang'."""

from __future__ import annotations

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AjaxDoorbellConfigEntry
from .entity import AjaxDoorbellEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the ring event when the device is a doorbell."""
    doorbell = entry.runtime_data
    if doorbell.is_doorbell:
        async_add_entities([AjaxDoorbellRingEvent(doorbell, "ring")])


class AjaxDoorbellRingEvent(AjaxDoorbellEntity, EventEntity):
    """Fires 'ring' within a second of the button press (local ONVIF event)."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]
    _attr_translation_key = "ring"

    async def async_added_to_hass(self) -> None:
        """Listen to presses."""
        await super().async_added_to_hass()
        self.async_on_remove(self._doorbell.async_add_ring_listener(self._ring))

    @callback
    def _ring(self) -> None:
        self._trigger_event("ring")
        self.async_write_ha_state()
