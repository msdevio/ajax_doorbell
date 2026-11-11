"""Binary sensors: incoming call, motion, PIR, AI detections, event stream health."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AjaxDoorbellConfigEntry
from .call import CallState
from .const import is_nvr_entry
from .doorbell import AjaxDoorbell
from .entity import AjaxDoorbellEntity
from .hub_entities import nvr_binary_sensors

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AjaxBinarySensorDescription(BinarySensorEntityDescription):
    """How one binary sensor reads its state and learns about changes."""

    is_on: Callable[[AjaxDoorbell], bool]
    subscribe: Callable[[AjaxDoorbell, Callable[[], None]], CALLBACK_TYPE]
    available: Callable[[AjaxDoorbell], bool] = lambda doorbell: True
    exists: Callable[[AjaxDoorbell], bool] = lambda doorbell: True


def _detection(
    key: str, device_class: BinarySensorDeviceClass | None = None
) -> AjaxBinarySensorDescription:
    return AjaxBinarySensorDescription(
        key=key,
        translation_key=key,
        device_class=device_class,
        is_on=lambda doorbell: doorbell.detections.state[key],
        subscribe=lambda doorbell, cb: doorbell.detections.async_add_listener(cb),
        available=lambda doorbell: doorbell.events.connected,
        exists=_DETECTION_EXISTS.get(key, lambda doorbell: doorbell.has_object_detection),
    )


_DETECTION_EXISTS: dict[str, Callable[[AjaxDoorbell], bool]] = {
    "motion": lambda doorbell: True,
    "pir": lambda doorbell: doorbell.has_pir,
}


DESCRIPTIONS: tuple[AjaxBinarySensorDescription, ...] = (
    AjaxBinarySensorDescription(
        key="ringing",
        translation_key="ringing",
        is_on=lambda doorbell: doorbell.call.state is CallState.RINGING,
        subscribe=lambda doorbell, cb: doorbell.call.async_add_listener(cb),
        exists=lambda doorbell: doorbell.is_doorbell,
    ),
    _detection("motion", BinarySensorDeviceClass.MOTION),
    _detection("pir", BinarySensorDeviceClass.MOTION),
    _detection("person"),
    _detection("vehicle"),
    _detection("pet"),
    AjaxBinarySensorDescription(
        key="event_stream",
        translation_key="event_stream",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on=lambda doorbell: doorbell.events.connected,
        subscribe=lambda doorbell, cb: doorbell.async_add_connection_listener(cb),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensors this device supports."""
    if is_nvr_entry(entry):
        async_add_entities(nvr_binary_sensors(entry.runtime_data))
        return
    doorbell = entry.runtime_data
    async_add_entities(
        AjaxBinarySensor(doorbell, description)
        for description in DESCRIPTIONS
        if description.exists(doorbell)
    )


class AjaxBinarySensor(AjaxDoorbellEntity, BinarySensorEntity):
    """A pushed binary sensor."""

    entity_description: AjaxBinarySensorDescription

    def __init__(self, doorbell: AjaxDoorbell, description: AjaxBinarySensorDescription) -> None:
        """Initialize the sensor."""
        super().__init__(doorbell, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return the state."""
        return self.entity_description.is_on(self._doorbell)

    @property
    def available(self) -> bool:
        """Return False while the value cannot be trusted."""
        return self.entity_description.available(self._doorbell)

    async def async_added_to_hass(self) -> None:
        """Follow changes."""
        await super().async_added_to_hass()
        self.async_on_remove(self.entity_description.subscribe(self._doorbell, self._update))
        # Availability of detections follows the event stream.
        if self.entity_description.key not in ("ringing", "event_stream"):
            self.async_on_remove(self._doorbell.async_add_connection_listener(self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()
