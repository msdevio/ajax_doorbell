"""Entities of the cameras an Ajax NVR records (and of the NVR itself)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.entity import Entity

from .const import MODE_CLIPS, MODE_CONTINUOUS, MODE_NOT_RECORDED, MODE_UNKNOWN
from .hub import AjaxNvrHub, Channel


class AjaxNvrEntity(Entity):
    """An entity of the NVR, or of one of its cameras."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: AjaxNvrHub, channel: Channel | None, key: str) -> None:
        """Attach the entity to its camera's device, or to the NVR's."""
        self._hub = hub
        self._channel = channel
        if channel is None:
            self._attr_unique_id = f"{hub.unique_base}_{key}"
            self._attr_device_info = hub.device_info
        else:
            self._attr_unique_id = f"{hub.unique_base}_{channel.group}_{key}"
            self._attr_device_info = hub.channel_device_info(channel)

    @property
    def channel(self) -> Channel:
        """Return the camera (only for camera entities)."""
        assert self._channel is not None
        return self._channel


# ------------------------------------------------------------------- camera


class AjaxNvrCamera(AjaxNvrEntity, Camera):
    """A camera's live stream, re-streamed by the NVR through the local relay."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, hub: AjaxNvrHub, channel: Channel, main: bool) -> None:
        """Initialize the camera."""
        AjaxNvrEntity.__init__(self, hub, channel, "camera" if main else "camera_sub")
        Camera.__init__(self)
        self._quality = "main" if main else "sub"
        self._attr_brand = "Ajax"
        if main:
            self._attr_name = None
        else:
            self._attr_translation_key = "substream"
            self._attr_entity_registry_enabled_default = False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Describe the stream."""
        profile = self.channel.profiles.get(self._quality)
        if profile is None:
            return {}
        return {
            "resolution": f"{profile.width}x{profile.height}" if profile.pixels else None,
            "frame_rate": profile.fps,
            "codec": profile.encoding,
            "audio_codec": profile.audio_encoding,
            "via": "nvr",
        }

    async def stream_source(self) -> str | None:
        """Return the local relay URL for go2rtc."""
        return self._hub.live_url(self.channel, self._quality)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image."""
        return await self._hub.async_snapshot(self, width, height)


def nvr_cameras(hub: AjaxNvrHub) -> list[Camera]:
    """Return the cameras: main stream, plus the substream (disabled by default)."""
    entities: list[Camera] = []
    for channel in hub.channels.values():
        if "main" in channel.stream_uris:
            entities.append(AjaxNvrCamera(hub, channel, main=True))
        if "sub" in channel.stream_uris:
            entities.append(AjaxNvrCamera(hub, channel, main=False))
    return entities


# ------------------------------------------------------------ binary sensors


@dataclass(frozen=True, kw_only=True)
class AjaxNvrBinarySensorDescription(BinarySensorEntityDescription):
    """How a camera's binary sensor reads its state and learns about changes."""

    is_on: Callable[[Channel], bool | None]
    subscribe: Callable[[Channel, Callable[[], None]], CALLBACK_TYPE]


def _detection(
    key: str, device_class: BinarySensorDeviceClass | None = None
) -> AjaxNvrBinarySensorDescription:
    return AjaxNvrBinarySensorDescription(
        key=key,
        translation_key=key,
        device_class=device_class,
        is_on=lambda channel: channel.detections.state[key],
        subscribe=lambda channel, cb: channel.detections.async_add_listener(cb),
    )


CAMERA_BINARY_SENSORS: tuple[AjaxNvrBinarySensorDescription, ...] = (
    _detection("motion", BinarySensorDeviceClass.MOTION),
    _detection("person"),
    _detection("vehicle"),
    _detection("pet"),
    AjaxNvrBinarySensorDescription(
        key="recording",
        translation_key="recording",
        device_class=BinarySensorDeviceClass.RUNNING,
        is_on=lambda channel: channel.recording,
        subscribe=lambda channel, cb: channel.async_add_listener(cb),
    ),
)


class AjaxNvrCameraBinarySensor(AjaxNvrEntity, BinarySensorEntity):
    """A camera's detection, or whether the NVR records it right now."""

    entity_description: AjaxNvrBinarySensorDescription

    def __init__(
        self, hub: AjaxNvrHub, channel: Channel, description: AjaxNvrBinarySensorDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(hub, channel, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        return self.entity_description.is_on(self.channel)

    @property
    def available(self) -> bool:
        """Follow the NVR's event stream."""
        return self._hub.events.connected

    async def async_added_to_hass(self) -> None:
        """Follow changes."""
        await super().async_added_to_hass()
        self.async_on_remove(self.entity_description.subscribe(self.channel, self._update))
        self.async_on_remove(self._hub.async_add_connection_listener(self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class AjaxNvrConnectedSensor(AjaxNvrEntity, BinarySensorEntity):
    """Whether the NVR's event stream is up."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "event_stream"

    @property
    def is_on(self) -> bool:
        """Return True while events arrive."""
        return self._hub.events.connected

    async def async_added_to_hass(self) -> None:
        """Follow the connection."""
        await super().async_added_to_hass()
        self.async_on_remove(self._hub.async_add_connection_listener(self.async_write_ha_state))


def nvr_binary_sensors(hub: AjaxNvrHub) -> list[BinarySensorEntity]:
    """Return the NVR's and its cameras' binary sensors."""
    entities: list[BinarySensorEntity] = [AjaxNvrConnectedSensor(hub, None, "event_stream")]
    for channel in hub.channels.values():
        entities += [
            AjaxNvrCameraBinarySensor(hub, channel, description)
            for description in CAMERA_BINARY_SENSORS
        ]
    return entities


# ------------------------------------------------------------------ sensors


class AjaxNvrRecordingModeSensor(AjaxNvrEntity, SensorEntity):
    """How the NVR records a camera, as measured on its recordings."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = [MODE_CONTINUOUS, MODE_CLIPS, MODE_UNKNOWN, MODE_NOT_RECORDED]
    _attr_translation_key = "recording_mode"

    @property
    def native_value(self) -> str:
        """Return the mode."""
        return self.channel.mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return when it was measured and what the replays showed."""
        analysis = self.channel.analysis
        return {
            "measured": analysis.get("at"),
            "replays": [
                f"{probe['asked'][11:19]} -> "
                + (
                    f"{probe['plays'][11:19]} ({probe['delta_s']:+.0f} s)"
                    if probe["plays"]
                    else probe.get("error") or "nothing"
                )
                for probe in analysis.get("probes", [])
            ],
        }

    async def async_added_to_hass(self) -> None:
        """Follow new measures."""
        await super().async_added_to_hass()
        self.async_on_remove(self.channel.async_add_listener(self.async_write_ha_state))


def nvr_sensors(hub: AjaxNvrHub) -> list[SensorEntity]:
    """Return each camera's recording mode."""
    return [
        AjaxNvrRecordingModeSensor(hub, channel, "recording_mode")
        for channel in hub.channels.values()
    ]
