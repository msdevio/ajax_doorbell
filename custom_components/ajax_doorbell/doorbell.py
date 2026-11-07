"""Runtime object for one Ajax DoorBell: discovery of its streams and events."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .call import CallManager
from .const import (
    CONF_CALL_TIMEOUT,
    CONF_RING_DURATION,
    CONF_RTSP_PORT,
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_RING_DURATION,
    DEFAULT_RTSP_PORT,
    DETECTION_HOLD,
    DOMAIN,
    MANUFACTURER,
    RING_DEBOUNCE,
    TALK_CODECS,
    TOPIC_MOTION,
    TOPIC_OBJECT,
    TOPIC_RING,
)
from .events import EventStream
from .onvif import AjaxOnvifClient, DeviceIdentity, MediaProfile, OnvifError, OnvifEvent
from .rtsp import RtspError, SdpMedia, StreamDescription, async_describe

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

DETECTIONS = ("motion", "person", "vehicle", "pet")
_OBJECT_CLASSES = {
    "human": "person",
    "person": "person",
    "vehicle": "vehicle",
    "car": "vehicle",
    "truck": "vehicle",
    "bus": "vehicle",
    "bicycle": "vehicle",
    "motorcycle": "vehicle",
    "animal": "pet",
    "pet": "pet",
    "dog": "pet",
    "cat": "pet",
}


def _matches(topic: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in topic for pattern in patterns)


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "1", "on", "yes")


def model_name(identity: DeviceIdentity | None, is_doorbell: bool) -> str:
    """Return a friendly model name: ONVIF reports a board name."""
    if is_doorbell:
        return "DoorBell"
    return identity.model if identity else "Video device"


class Detections:
    """Motion and AI detections, each with a safety auto-off."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize everything as off."""
        self._hass = hass
        self.state: dict[str, bool] = dict.fromkeys(DETECTIONS, False)
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._listeners: list[Callable[[], None]] = []

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener on every change."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def set(self, key: str, value: bool) -> None:
        """Record a detection; an 'on' that never clears turns off by itself."""
        if (cancel := self._timers.pop(key, None)) is not None:
            cancel()
        if value:

            @callback
            def _hold_expired(_now: datetime) -> None:
                self._timers.pop(key, None)
                self.set(key, False)

            self._timers[key] = async_call_later(self._hass, DETECTION_HOLD, _hold_expired)
        if self.state.get(key) != value:
            self.state[key] = value
            for listener in list(self._listeners):
                listener()

    @callback
    def shutdown(self) -> None:
        """Cancel the auto-off timers."""
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()


class AjaxDoorbell:
    """Everything the entities of one DoorBell share."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Set up the client; nothing is contacted yet."""
        self.hass = hass
        self.entry = entry
        data = entry.data
        self.client = AjaxOnvifClient(
            async_get_clientsession(hass),
            data[CONF_HOST],
            data[CONF_PORT],
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
        )
        self.identity: DeviceIdentity | None = None
        self.mac: str | None = None
        self.profiles: list[MediaProfile] = []
        self.main_profile: MediaProfile | None = None
        self.sub_profile: MediaProfile | None = None
        self.stream_uris: dict[str, str] = {}
        self.snapshot_uri: str | None = None
        self.topics: list[str] = []
        self.description: StreamDescription | None = None
        self.backchannel: SdpMedia | None = None
        self.last_events: deque[dict[str, Any]] = deque(maxlen=25)
        self._last_ring = 0.0
        self.call = CallManager(
            hass,
            self._event_data,
            entry.options.get(CONF_RING_DURATION, DEFAULT_RING_DURATION),
            entry.options.get(CONF_CALL_TIMEOUT, DEFAULT_CALL_TIMEOUT),
        )
        self.detections = Detections(hass)
        self.events = EventStream(
            hass, self.client, self._on_event, self._on_connection, self._on_auth_error
        )
        self._ring_listeners: list[Callable[[], None]] = []
        self._connection_listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ setup

    async def async_initialize(self) -> None:
        """Read identity, streams, topics and the talk-back track."""
        client = self.client
        await client.async_update_time_offset()
        try:
            await client.async_get_services()
        except OnvifError as err:
            _LOGGER.debug("GetServices failed (%s), using default paths", err)
        self.identity = await client.async_get_identity()
        try:
            self.mac = await client.async_get_mac()
        except OnvifError:
            self.mac = None
        self.profiles = await client.async_get_profiles()
        video = sorted((p for p in self.profiles if p.pixels), key=lambda p: -p.pixels)
        if not video:
            video = list(self.profiles)
        if not video:
            raise OnvifError(f"{client.host}: no media profile")
        self.main_profile = video[0]
        self.sub_profile = video[-1] if len(video) > 1 else None
        for profile in filter(None, (self.main_profile, self.sub_profile)):
            self.stream_uris[profile.token] = await client.async_get_stream_uri(profile.token)
        self.snapshot_uri = await client.async_get_snapshot_uri(self.main_profile.token)
        try:
            self.topics = await client.async_get_topics()
        except OnvifError as err:
            _LOGGER.debug("GetEventProperties failed: %s", err)
        await self.async_probe_backchannel()

    async def async_probe_backchannel(self) -> None:
        """Ask the main stream for its tracks, including the talk-back one."""
        if self.main_profile is None:
            return
        data = self.entry.data
        try:
            self.description = await async_describe(
                self.stream_url(self.main_profile, with_credentials=False),
                data[CONF_USERNAME],
                data[CONF_PASSWORD],
                backchannel=True,
            )
        except RtspError as err:
            _LOGGER.debug("Backchannel probe failed: %s", err)
            return
        self.backchannel = self.description.backchannel
        _LOGGER.debug(
            "Stream %s: video %s, audio %s, backchannel %s",
            self.description.status,
            self.description.video and self.description.video.codecs,
            self.description.audio and self.description.audio.codecs,
            self.backchannel and self.backchannel.codecs,
        )

    def start(self, entry: ConfigEntry) -> None:
        """Start listening to events."""
        self.events.start(entry)

    async def async_shutdown(self) -> None:
        """Stop everything."""
        await self.events.async_stop()
        self.call.shutdown()
        self.detections.shutdown()

    # -------------------------------------------------------------- properties

    @property
    def unique_base(self) -> str:
        """Return the stable id the device and entity ids are built on."""
        if self.entry.unique_id:
            return self.entry.unique_id
        return self.identity.serial if self.identity else self.entry.entry_id

    @property
    def is_doorbell(self) -> bool:
        """Return True when the device sends ring events (or we cannot tell)."""
        return not self.topics or any(_matches(t, TOPIC_RING) for t in self.topics)

    @property
    def has_object_detection(self) -> bool:
        """Return True when the device sends AI detections (or we cannot tell)."""
        return not self.topics or any(_matches(t, TOPIC_OBJECT) for t in self.topics)

    @property
    def two_way_audio(self) -> bool:
        """Return True when go2rtc can feed the backchannel without transcoding."""
        return bool(self.backchannel and self.backchannel.codec_names & TALK_CODECS)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry entry."""
        identity = self.identity
        info = DeviceInfo(
            identifiers={(DOMAIN, self.unique_base)},
            manufacturer=MANUFACTURER,
            model=model_name(identity, self.is_doorbell),
            model_id=identity.model if identity else None,
            name=self.entry.title,
            serial_number=identity.serial if identity else None,
            sw_version=identity.firmware if identity else None,
        )
        if self.mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, dr.format_mac(self.mac))}
        return info

    def stream_url(self, profile: MediaProfile, *, with_credentials: bool = True) -> str:
        """Return the RTSP URL of a profile, aimed at the configured host."""
        parts = urlsplit(self.stream_uris[profile.token])
        port = parts.port or self.entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
        netloc = f"{self.client.host}:{port}"
        if with_credentials:
            data = self.entry.data
            user = quote(data[CONF_USERNAME], safe="")
            password = quote(data[CONF_PASSWORD], safe="")
            netloc = f"{user}:{password}@{netloc}"
        return urlunsplit(parts._replace(netloc=netloc))

    # --------------------------------------------------------------- snapshot

    async def async_snapshot(
        self, camera: Camera, width: int | None, height: int | None
    ) -> bytes | None:
        """Return a JPEG: the device's own snapshot, else a frame from go2rtc."""
        if self.snapshot_uri:
            try:
                return await self.client.async_get_bytes(self.snapshot_uri)
            except OnvifError as err:
                _LOGGER.debug("Snapshot URI failed (%s), using the stream", err)
        provider = camera.webrtc_provider
        if provider is not None and hasattr(provider, "async_get_image"):
            return await provider.async_get_image(camera, width, height)
        return None

    # ----------------------------------------------------------------- events

    def _event_data(self) -> dict[str, Any]:
        device = dr.async_get(self.hass).async_get_device_by_identifier(
            (DOMAIN, self.unique_base), self.entry.entry_id
        )
        return {
            "device_id": device.id if device else None,
            "config_entry_id": self.entry.entry_id,
        }

    @callback
    def async_add_ring_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener on every button press."""
        self._ring_listeners.append(listener)
        return lambda: self._ring_listeners.remove(listener)

    @callback
    def async_add_connection_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener when the event stream connects or drops."""
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    @callback
    def _on_connection(self, _connected: bool) -> None:
        for listener in list(self._connection_listeners):
            listener()

    @callback
    def _on_auth_error(self) -> None:
        self.entry.async_start_reauth(self.hass)

    @callback
    def _on_event(self, event: OnvifEvent) -> None:
        self.last_events.append(
            {
                "received": dt_util.utcnow().isoformat(),
                "sent": event.utc_time.isoformat() if event.utc_time else None,
                "topic": event.topic,
                "operation": event.operation,
                "data": event.data,
            }
        )
        topic = event.topic
        if _matches(topic, TOPIC_RING):
            self._handle_ring(event)
        elif _matches(topic, TOPIC_MOTION):
            state = next(
                (event.data[k] for k in ("State", "IsMotion", "Detected") if k in event.data),
                None,
            )
            self.detections.set("motion", _is_true(state))
        elif _matches(topic, TOPIC_OBJECT):
            raw = event.data.get("ClassTypes", event.data.get("ClassType", ""))
            found = {_OBJECT_CLASSES.get(c.strip().lower()) for c in raw.split(",")}
            for key in ("person", "vehicle", "pet"):
                self.detections.set(key, key in found)
        else:
            _LOGGER.debug("Unhandled event %s %s", topic, event.data)

    @callback
    def _handle_ring(self, event: OnvifEvent) -> None:
        pressed = _is_true(
            next(
                (event.data[k] for k in ("Detected", "State", "Value") if k in event.data),
                "true",  # a ring message without a value is a press
            )
        )
        # The synchronization point replays the current value on every
        # (re)subscription; only real changes are presses.
        if not pressed or event.operation == "Initialized":
            return
        now = time.monotonic()
        if now - self._last_ring < RING_DEBOUNCE:
            return
        self._last_ring = now
        delay = ""
        if event.utc_time is not None:
            late = (dt_util.utcnow() - event.utc_time + self.client.time_offset).total_seconds()
            if 0 <= late < 60:  # only meaningful when both clocks agree
                delay = f" ({late * 1000:.0f} ms after the press)"
        _LOGGER.info("%s: doorbell pressed%s", self.entry.title, delay)
        self.call.ring()
        for listener in list(self._ring_listeners):
            listener()
