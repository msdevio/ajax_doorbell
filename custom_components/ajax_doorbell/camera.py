"""Camera entities: live video through Home Assistant's go2rtc, with talk-back."""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AjaxDoorbellConfigEntry
from .const import is_nvr_entry
from .doorbell import AjaxDoorbell
from .entity import AjaxDoorbellEntity
from .hub_entities import nvr_cameras
from .onvif import MediaProfile

try:  # Home Assistant 2026.11 and later
    from homeassistant.components.camera import CameraEntityCapabilities
except ImportError:  # the intercom card still talks on 2026.10
    CameraEntityCapabilities = None  # type: ignore[assignment,misc]

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxDoorbellConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the main stream camera, plus the substream (disabled by default)."""
    if is_nvr_entry(entry):
        async_add_entities(nvr_cameras(entry.runtime_data))
        return
    doorbell = entry.runtime_data
    assert doorbell.main_profile is not None
    entities: list[Camera] = [AjaxDoorbellCamera(doorbell, doorbell.main_profile, main=True)]
    if doorbell.sub_profile is not None:
        entities.append(AjaxDoorbellCamera(doorbell, doorbell.sub_profile, main=False))
    if doorbell.nvr_configured:
        entities.append(AjaxReplayCamera(doorbell))
    async_add_entities(entities)


class AjaxDoorbellCamera(AjaxDoorbellEntity, Camera):
    """One RTSP stream of the DoorBell.

    Home Assistant hands the RTSP URL to its built-in go2rtc, which serves the
    browser over WebRTC: video is copied as is (no re-encoding, lowest
    latency), G.722 audio becomes Opus, and the browser microphone goes back
    to the doorbell over the ONVIF backchannel.
    """

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, doorbell: AjaxDoorbell, profile: MediaProfile, main: bool) -> None:
        """Initialize the camera."""
        AjaxDoorbellEntity.__init__(self, doorbell, "camera" if main else "camera_sub")
        Camera.__init__(self)
        self._profile = profile
        self._attr_brand = "Ajax"
        self._attr_model = doorbell.device_info.get("model")
        if main:
            self._attr_name = None
            if CameraEntityCapabilities is not None:
                self._attr_camera_entity_capabilities = CameraEntityCapabilities(
                    two_way_audio=doorbell.two_way_audio
                )
        else:
            self._attr_translation_key = "substream"
            self._attr_entity_registry_enabled_default = False

    async def async_added_to_hass(self) -> None:
        """Follow a talk-back capability learnt after setup."""
        await super().async_added_to_hass()
        if self._attr_name is None:  # main stream
            self.async_on_remove(
                self._doorbell.async_add_capability_listener(self._capabilities_changed)
            )

    @callback
    def _capabilities_changed(self) -> None:
        if CameraEntityCapabilities is not None:
            self._attr_camera_entity_capabilities = CameraEntityCapabilities(
                two_way_audio=self._doorbell.two_way_audio
            )
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, str | int | float | None]:
        """Describe the stream."""
        profile = self._profile
        return {
            "resolution": f"{profile.width}x{profile.height}" if profile.pixels else None,
            "frame_rate": profile.fps,
            "codec": profile.encoding,
            "audio_codec": profile.audio_encoding,
        }

    async def stream_source(self) -> str | None:
        """Return the RTSP URL (with credentials) for go2rtc."""
        return self._doorbell.stream_url(self._profile)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image."""
        return await self._doorbell.async_snapshot(self, width, height)


class AjaxReplayCamera(AjaxDoorbellEntity, Camera):
    """The DoorBell's recordings, replayed from the NVR at the moment the card picks.

    Nothing is stored: go2rtc plays the NVR's recording through the
    integration's local relay, as it plays the live stream.
    """

    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_translation_key = "replay"
    _attr_entity_registry_visible_default = False

    def __init__(self, doorbell: AjaxDoorbell) -> None:
        """Initialize the replay camera."""
        AjaxDoorbellEntity.__init__(self, doorbell, "replay")
        Camera.__init__(self)
        self._attr_brand = "Ajax"

    async def async_added_to_hass(self) -> None:
        """Follow the NVR and the moment being replayed."""
        await super().async_added_to_hass()
        self.async_on_remove(self._doorbell.async_add_replay_listener(self._replay_changed))

    @callback
    def _replay_changed(self) -> None:
        # Home Assistant picks a camera's WebRTC provider from its stream source,
        # which only exists once the NVR answers: ask it again.
        self.hass.async_create_task(self.async_refresh_providers())
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return True once the NVR answers."""
        return self._doorbell.nvr is not None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return the moment being replayed."""
        start = self._doorbell.replay_start
        return {"replay_start": start.isoformat() if start else None}

    async def stream_source(self) -> str | None:
        """Return the local relay URL of the recording at the chosen moment."""
        nvr, start = self._doorbell.nvr, self._doorbell.replay_start
        if nvr is None or start is None:
            return None
        return nvr.replay_url(start)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the recording's image at the chosen moment."""
        nvr, start = self._doorbell.nvr, self._doorbell.replay_start
        if nvr is None or start is None:
            return None
        return await nvr.async_thumbnail(start)
