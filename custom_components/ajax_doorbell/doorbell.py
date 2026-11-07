"""Runtime object for one Ajax DoorBell: discovery of its streams and events."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime
import logging
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .call import CallManager, CallState
from .const import (
    CONF_CALL_TIMEOUT,
    CONF_NVR_HOST,
    CONF_NVR_PASSWORD,
    CONF_NVR_PORT,
    CONF_NVR_USERNAME,
    CONF_RING_DURATION,
    CONF_VISIT_RETENTION,
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_ONVIF_PORT,
    DEFAULT_RING_DURATION,
    DEFAULT_RTSP_PORT,
    DEFAULT_VISIT_RETENTION,
    DETECTION_HOLD,
    DOMAIN,
    MANUFACTURER,
    RING_DEBOUNCE,
    TALK_CODECS,
    TOPIC_MOTION,
    TOPIC_OBJECT,
    TOPIC_PIR,
    TOPIC_RING,
    TOPIC_TRACKING,
)
from .events import EventStream
from .nvr import AjaxNvr, NvrError
from .onvif import (
    AjaxOnvifClient,
    DeviceIdentity,
    MediaProfile,
    OnvifAuthError,
    OnvifError,
    OnvifEvent,
    host_port,
)
from .rtsp import RtspError, SdpMedia, StreamDescription, async_describe
from .talk import TALK_ENCODINGS, TalkSession
from .visits import VisitLog

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

DETECTIONS = ("motion", "pir", "person", "vehicle", "pet")
# AI detections turn off by themselves after DETECTION_HOLD seconds should the
# device's "clear" ever be lost; motion and PIR are repeated every few seconds.
HELD_DETECTIONS = frozenset({"person", "vehicle", "pet"})
USED_TOPICS = TOPIC_RING + TOPIC_PIR + TOPIC_MOTION + TOPIC_OBJECT
_PREFIX = re.compile(r"(^|/)[\w.-]+:")
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


def normalize_topic(topic: str) -> str:
    """Drop the namespace prefix of every segment: tnsajax:X/tns1:Y -> X/Y."""
    return _PREFIX.sub(r"\1", topic.strip())


def _matches(topic: str, patterns: tuple[str, ...]) -> bool:
    plain = normalize_topic(topic)
    return any(plain == pattern or plain.endswith(f"/{pattern}") for pattern in patterns)


def _value(data: dict[str, str], *names: str) -> str | None:
    return next((data[name] for name in names if name in data), None)


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "1", "on", "yes")


def model_name(identity: DeviceIdentity | None, is_doorbell: bool) -> str:
    """Return a friendly model name: ONVIF reports a board name."""
    if is_doorbell:
        return "DoorBell"
    return identity.model if identity else "Video device"


class Detections:
    """Motion, PIR and AI detections, each on while any rule reporting it says so."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize everything as off."""
        self._hass = hass
        self.state: dict[str, bool] = dict.fromkeys(DETECTIONS, False)
        self._active: dict[str, set[str]] = {key: set() for key in DETECTIONS}
        self._timers: dict[tuple[str, str], CALLBACK_TYPE] = {}
        self._listeners: list[Callable[[], None]] = []

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener on every change."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def set(self, key: str, value: bool, source: str = "") -> None:
        """Record what one source (topic and rule) says about a detection."""
        if (cancel := self._timers.pop((key, source), None)) is not None:
            cancel()
        active = self._active[key]
        if value:
            active.add(source)
            if key in HELD_DETECTIONS:

                @callback
                def _hold_expired(_now: datetime) -> None:
                    self._timers.pop((key, source), None)
                    self.set(key, False, source)

                self._timers[key, source] = async_call_later(
                    self._hass, DETECTION_HOLD, _hold_expired
                )
        else:
            active.discard(source)
        if self.state[key] != bool(active):
            self.state[key] = bool(active)
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
        self._last_values: dict[str, tuple[tuple[str, str], ...]] = {}
        self._unhandled: set[str] = set()
        self._ring_held: dict[str, bool] = {}
        self._last_ring = 0.0
        self.call = CallManager(
            hass,
            self._event_data,
            entry.options.get(CONF_RING_DURATION, DEFAULT_RING_DURATION),
            entry.options.get(CONF_CALL_TIMEOUT, DEFAULT_CALL_TIMEOUT),
        )
        self.detections = Detections(hass)
        self.visits = VisitLog(
            hass,
            entry.entry_id,
            int(entry.options.get(CONF_VISIT_RETENTION, DEFAULT_VISIT_RETENTION)),
        )
        self.nvr: AjaxNvr | None = None
        self.nvr_error: str | None = None
        self._nvr_task: asyncio.Task | None = None
        self.replay_start: datetime | None = None
        self._replay_listeners: list[Callable[[], None]] = []
        self.events = EventStream(
            hass,
            self.client,
            self._on_event,
            self._on_connection,
            self._async_events_refused,
            self._events_recovered,
        )
        self._ring_listeners: list[Callable[[], None]] = []
        self._connection_listeners: list[Callable[[], None]] = []
        self._capability_listeners: list[Callable[[], None]] = []
        self._reprobe: asyncio.Task | None = None
        self.talk: TalkSession | None = None
        self._talk_lock = asyncio.Lock()
        self._call_state = self.call.state
        self.call.async_add_listener(self._call_changed)

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
        self.events.topics = self.subscription_topics
        await self.visits.async_load()
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
        """Start listening to events, and reach the NVR in the background."""
        self.events.start(entry)
        if self.nvr_configured:
            self._nvr_task = entry.async_create_background_task(
                self.hass, self._async_connect_nvr(), "ajax_doorbell nvr"
            )

    async def async_shutdown(self) -> None:
        """Stop everything."""
        if self._reprobe is not None:
            self._reprobe.cancel()
        if self._nvr_task is not None:
            self._nvr_task.cancel()
        if self.nvr is not None:
            await self.nvr.async_close()
        await self.events.async_stop()
        if self.talk is not None:
            await self.async_stop_talk(self.talk, "unloaded")
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
    def has_pir(self) -> bool:
        """Return True when the device has a PIR sensor (or we cannot tell)."""
        return not self.topics or any(_matches(t, TOPIC_PIR) for t in self.topics)

    @property
    def subscription_topics(self) -> list[str]:
        """Return the advertised topics the integration uses, to filter the subscription.

        The tracking topic is left out when the standard object topic exists:
        it only repeats the same detections, as boxes, several times a second.
        """
        standard_objects = any(_matches(t, TOPIC_OBJECT[:1]) for t in self.topics)
        return [
            topic
            for topic in self.topics
            if _matches(topic, USED_TOPICS)
            and not (standard_objects and _matches(topic, TOPIC_TRACKING))
        ]

    @property
    def two_way_audio(self) -> bool:
        """Return True when go2rtc itself can feed the backchannel (Home Assistant's Talk)."""
        return bool(self.backchannel and self.backchannel.codec_names & TALK_CODECS)

    @property
    def talk_codec(self) -> str | None:
        """Return the backchannel codec the card can encode for the relay, if any."""
        if self.backchannel is None:
            return None
        return next((c for c in TALK_ENCODINGS if c in self.backchannel.codec_names), None)

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
        """Return the RTSP URL of a profile, aimed at the configured host.

        The port is the one the device announces in GetStreamUri.
        """
        parts = urlsplit(self.stream_uris[profile.token])
        netloc = host_port(self.client.host, parts.port or DEFAULT_RTSP_PORT)
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
            try:
                return await provider.async_get_image(camera, width, height)
            except Exception as err:
                _LOGGER.debug("go2rtc could not grab a frame: %s", err)
        return None

    # ------------------------------------------------------------------- NVR

    @property
    def nvr_configured(self) -> bool:
        """Return True when the options name an NVR."""
        return bool(self.entry.options.get(CONF_NVR_HOST))

    async def _async_connect_nvr(self) -> None:
        """Reach the NVR; retry slowly while it is away (it may be unplugged)."""
        options, data = self.entry.options, self.entry.data
        delay = 60
        while True:
            nvr = AjaxNvr(
                self.hass,
                options[CONF_NVR_HOST],
                int(options.get(CONF_NVR_PORT) or DEFAULT_ONVIF_PORT),
                options.get(CONF_NVR_USERNAME) or data[CONF_USERNAME],
                options.get(CONF_NVR_PASSWORD) or data[CONF_PASSWORD],
                self.mac,
            )
            try:
                await nvr.async_initialize()
            except (OnvifError, NvrError, OSError) as err:
                await nvr.async_close()
                if self.nvr_error is None:
                    _LOGGER.warning("%s: recordings unavailable: %s", self.entry.title, err)
                self.nvr_error = str(err)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 900)
                continue
            self.nvr, self.nvr_error = nvr, None
            _LOGGER.info("%s: recordings available from the NVR %s", self.entry.title, nvr.host)
            self._notify_replay()
            return

    @callback
    def async_add_replay_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener when the NVR becomes available or the replay moves."""
        self._replay_listeners.append(listener)
        return lambda: self._replay_listeners.remove(listener)

    @callback
    def _notify_replay(self) -> None:
        for listener in list(self._replay_listeners):
            listener()

    @callback
    def set_replay(self, start: datetime) -> None:
        """Point the replay camera at a moment of the recordings."""
        if self.nvr is None:
            raise NvrError("the NVR is not available")
        self.replay_start = start
        self._notify_replay()

    # ------------------------------------------------------------------- talk

    async def async_start_talk(self, owner: str) -> TalkSession:
        """Open the speaker for one talker, taking it from any other."""
        if self.main_profile is None:
            raise RtspError(f"{self.client.host}: no stream")
        async with self._talk_lock:
            if (previous := self.talk) is not None:
                self.talk = None
                await previous.async_close("taken_over")
            data = self.entry.data
            session = TalkSession(
                self.stream_url(self.main_profile, with_credentials=False),
                data[CONF_USERNAME],
                data[CONF_PASSWORD],
                owner,
            )
            await session.async_open()
            self.talk = session
        _LOGGER.debug("%s: %s talks to the visitor", self.entry.title, owner)
        return session

    async def async_stop_talk(self, session: TalkSession, reason: str = "closed") -> None:
        """Close one talker's session."""
        if self.talk is session:
            self.talk = None
        await session.async_close(reason)

    @callback
    def _call_changed(self) -> None:
        previous, self._call_state = self._call_state, self.call.state
        if self._call_state is CallState.IDLE and previous is not CallState.IDLE and self.talk:
            # Hung up (or timed out) from any screen: the speaker closes too.
            self.entry.async_create_background_task(
                self.hass, self.async_stop_talk(self.talk, "hung_up"), "ajax_doorbell hang up"
            )

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
    def async_add_capability_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener when the talk-back capability is learnt later."""
        self._capability_listeners.append(listener)
        return lambda: self._capability_listeners.remove(listener)

    @callback
    def _on_connection(self, connected: bool) -> None:
        for listener in list(self._connection_listeners):
            listener()
        if connected and self.description is None and self._reprobe is None:
            # The stream probe failed at setup (doorbell busy or rebooting):
            # now that the device answers again, learn its tracks.
            self._reprobe = self.entry.async_create_background_task(
                self.hass, self._async_reprobe(), "ajax_doorbell backchannel probe"
            )

    async def _async_reprobe(self) -> None:
        try:
            await self.async_probe_backchannel()
        finally:
            self._reprobe = None
        if self.description is not None:
            for listener in list(self._capability_listeners):
                listener()

    @property
    def _issue_id(self) -> str:
        return f"event_permission_{self.entry.entry_id}"

    async def _async_events_refused(self) -> None:
        """Events keep being refused: wrong password, or a role without events?"""
        try:
            await self.client.async_get_profiles()
        except OnvifAuthError:
            self.entry.async_start_reauth(self.hass)
            return
        except OnvifError:
            return  # unreachable: the stream keeps retrying
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="event_permission",
            translation_placeholders={"name": self.entry.title},
        )

    @callback
    def _events_recovered(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)

    @callback
    def _on_event(self, event: OnvifEvent) -> None:
        topic = normalize_topic(event.topic)
        data = event.data
        if _matches(topic, TOPIC_TRACKING) and _value(data, "ClassTypes", "ClassType") is None:
            return  # live object tracking: boxes, several times a second
        # One source per topic and rule: the DoorBell repeats each one's state.
        source = "|".join((topic, *(f"{k}={v}" for k, v in sorted(event.source.items()))))
        values = tuple(sorted(data.items()))
        if self._last_values.get(source) != values:
            self._last_values[source] = values
            self.last_events.append(
                {
                    "received": dt_util.utcnow().isoformat(),
                    "sent": event.utc_time.isoformat() if event.utc_time else None,
                    "topic": event.topic,
                    "operation": event.operation,
                    "data": data,
                }
            )
        if _matches(topic, TOPIC_RING):
            self._handle_ring(event, source)
        elif _matches(topic, TOPIC_PIR):
            self.detections.set("pir", _is_true(_value(data, "Detected", "State")), source)
        elif _matches(topic, TOPIC_MOTION):
            state = _value(data, "State", "IsMotion", "Detected")
            self.detections.set("motion", _is_true(state), source)
        elif _matches(topic, TOPIC_OBJECT):
            raw = _value(data, "ClassTypes", "ClassType") or ""
            found = {_OBJECT_CLASSES.get(c.strip().lower()) for c in raw.split(",")}
            someone = self.detections.state["person"]
            for key in ("person", "vehicle", "pet"):
                self.detections.set(key, key in found, source)
            if self.detections.state["person"] and not someone and event.operation != "Initialized":
                self.visits.add("person")
        elif topic not in self._unhandled:
            self._unhandled.add(topic)
            _LOGGER.debug("Unhandled event topic %s: %s", event.topic, data)

    @callback
    def _handle_ring(self, event: OnvifEvent, source: str) -> None:
        value = _value(event.data, "Detected", "State", "Value")
        pressed = value is None or _is_true(value)  # a ring without a value is a press
        held = self._ring_held.get(source, False)
        if value is not None:
            self._ring_held[source] = pressed
        # The DoorBell repeats its state every few seconds and the synchronization
        # point replays it: only a change to "pressed" is a new press.
        if not pressed or held or event.operation == "Initialized":
            return
        now = time.monotonic()
        if now - self._last_ring < RING_DEBOUNCE:
            return
        self._last_ring = now
        self.call.ring()
        self.visits.add("ring")
        for listener in list(self._ring_listeners):
            listener()
        delay = ""
        if event.utc_time is not None:
            late = (dt_util.utcnow() - event.utc_time + self.client.time_offset).total_seconds()
            if 0 <= late < 60:  # only meaningful when both clocks agree
                delay = f" ({late * 1000:.0f} ms after the press)"
        _LOGGER.info("%s: doorbell pressed%s", self.entry.title, delay)
