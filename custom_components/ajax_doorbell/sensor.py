"""Sensors: call state, last ring and missed calls."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import AjaxDoorbellConfigEntry
from .call import CallState
from .entity import AjaxDoorbellEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the call sensors when the device is a doorbell."""
    doorbell = entry.runtime_data
    if not doorbell.is_doorbell:
        return
    async_add_entities(
        [
            CallStateSensor(doorbell, "call_state"),
            LastRingSensor(doorbell, "last_ring"),
            MissedCallsSensor(doorbell, "missed_calls"),
        ]
    )


class _CallSensor(AjaxDoorbellEntity):
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._doorbell.call.async_add_listener(self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class CallStateSensor(_CallSensor, SensorEntity):
    """idle, ringing or in_call: what the intercom card follows."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [state.value for state in CallState]
    _attr_translation_key = "call_state"

    async def async_added_to_hass(self) -> None:
        """Also follow the talk-back capability, learnt late if the DoorBell was busy."""
        await super().async_added_to_hass()
        self.async_on_remove(self._doorbell.async_add_capability_listener(self._update))

    @property
    def native_value(self) -> str:
        """Return the call state."""
        return self._doorbell.call.state.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return who answered and when."""
        call = self._doorbell.call
        return {
            "ring_started": call.ring_started,
            "answered_by": call.answered_by,
            "answered_at": call.answered_at,
            # The card talks through the integration's relay; see talk.py.
            "two_way_audio": self._doorbell.talk_codec is not None,
            "talk_via": "relay" if self._doorbell.talk_codec else None,
        }


class LastRingSensor(_CallSensor, RestoreSensor):
    """When the button was last pressed."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_ring"

    async def async_added_to_hass(self) -> None:
        """Restore the last ring across restarts."""
        await super().async_added_to_hass()
        if (data := await self.async_get_last_sensor_data()) and isinstance(
            data.native_value, datetime
        ):
            self._doorbell.call.restore(last_ring=data.native_value)

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the last press."""
        return self._doorbell.call.last_ring


class MissedCallsSensor(_CallSensor, RestoreSensor):
    """Rings nobody answered since the counter was last cleared."""

    _attr_translation_key = "missed_calls"

    async def async_added_to_hass(self) -> None:
        """Restore the counter across restarts."""
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        data = await self.async_get_last_sensor_data()
        last_missed = None
        if state is not None and (stamp := state.attributes.get("last_missed")):
            last_missed = dt_util.parse_datetime(str(stamp))
        if data is not None and data.native_value is not None:
            try:
                missed = int(data.native_value)
            except (TypeError, ValueError):
                missed = None
            self._doorbell.call.restore(missed_calls=missed, last_missed=last_missed)

    @property
    def native_value(self) -> int:
        """Return the number of missed calls."""
        return self._doorbell.call.missed_calls

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return when the last call was missed."""
        return {"last_missed": self._doorbell.call.last_missed}
