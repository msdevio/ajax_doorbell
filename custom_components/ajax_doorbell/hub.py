"""An Ajax NVR as a hub: one Home Assistant device per camera it records.

The NVR re-streams each camera, keeps their recordings (ONVIF Profile G) and
sends every camera's events on one subscription. Live video reaches go2rtc
through a small local relay: the NVR marks its tracks "sendrecv", which
go2rtc ignores, and the relay presents them as received only, as it does for
replays. Nothing is stored in Home Assistant.

The DoorBell keeps its own config entry and its direct connection: its
channel on the NVR is left out here, and none of its code runs for the NVR.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_RTSP_PORT,
    DOMAIN,
    MANUFACTURER,
    MODE_CLIPS,
    MODE_CONTINUOUS,
    MODE_NOT_RECORDED,
    MODE_UNKNOWN,
    TOPIC_JOB,
    TOPIC_MOTION,
    TOPIC_OBJECT,
    TOPIC_PIR,
    TOPIC_TRACKING,
    is_nvr_entry,
)
from .doorbell import _OBJECT_CLASSES, Detections, _is_true, _matches, _value, normalize_topic
from .events import EventStream
from .nvr import _ReplayRelay, rewrite_sdp
from .onvif import (
    AjaxOnvifClient,
    DeviceIdentity,
    MediaProfile,
    OnvifAuthError,
    OnvifError,
    OnvifEvent,
    ProfileDetails,
    Recording,
    host_port,
)
from .rtsp import RtspConnection, RtspError, media_url, parse_sdp, strip_credentials

if TYPE_CHECKING:
    from homeassistant.components.camera import Camera

_LOGGER = logging.getLogger(__name__)

NVR_PLATFORMS = [Platform.BINARY_SENSOR, Platform.CAMERA, Platform.SENSOR]

# The recording mode is measured by replaying from these moments (minutes ago):
# continuous recording plays at once, clips start later (the next event).
PROBE_MINUTES = (2, 15, 60, 180)
# Video starting this much after the asked moment belongs to a later clip.
JUMP_SECONDS = 10
PROBE_WAIT = 6
# The first measure comes shortly after setup, then one every six hours.
ANALYZE_DELAY = 30
ANALYZE_EVERY = timedelta(hours=6)
HISTORY_SPAN = timedelta(hours=6)
NTP_EPOCH = datetime(1900, 1, 1, tzinfo=UTC)

_LIVE_PATH = re.compile(r"^/live/([^/]+)/(main|sub)(/.*)?$")
_MAC = re.compile(r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])")


def channel_group(token: str) -> str:
    """Return the camera part of a profile or recording token.

    Ajax names them "<id>-<camera MAC>-0-main", "<id>-<camera MAC>-0-sub-r"...
    """
    return re.sub(r"[-_](main|sub)(-r)?$", "", token)


def is_sub(token: str) -> bool:
    """Return True for a substream profile or recording."""
    return re.search(r"[-_]sub(-r)?$", token) is not None


def channel_mac(group: str) -> str | None:
    """Return the camera's MAC address, which Ajax puts in its tokens."""
    match = _MAC.search(group.lower())
    return dr.format_mac(match.group(0)) if match else None


def channel_name(profile_name: str) -> str:
    """'Jardin _main' -> 'Jardin' (the name given in the Ajax app)."""
    return re.sub(r"\s*_?(main|sub)$", "", profile_name, flags=re.IGNORECASE).strip()


# Where a camera's name can come from, most trusted first.
NAME_SOURCES = ("profile", "recording", "video_source", "fallback")


def pick_names(candidates: dict[str, dict[str, str]]) -> dict[str, str]:
    """Name each camera from the first source that tells them all apart.

    The NVR repeats a camera's name in several places, which can disagree
    (an old name kept in one of them); duplicates are numbered as a last resort.
    """
    for source in NAME_SOURCES:
        names = {group: names.get(source, "").strip() for group, names in candidates.items()}
        values = [name.casefold() for name in names.values()]
        if all(values) and len(set(values)) == len(values):
            return names
    picked: dict[str, str] = {}
    seen: dict[str, int] = {}
    for index, (group, names) in enumerate(candidates.items(), 1):
        base = next((names[s].strip() for s in NAME_SOURCES if names.get(s, "").strip()), "")
        base = base or f"Camera {index}"
        seen[base.casefold()] = seen.get(base.casefold(), 0) + 1
        count = seen[base.casefold()]
        picked[group] = base if count == 1 else f"{base} {count}"
    return picked


def _toggles(history: list[tuple[datetime, bool]]) -> int:
    """Count the times a recording stopped holding video."""
    return sum(1 for (_, before), (_, after) in pairwise(history) if before and not after)


def recording_mode(
    probes: list[dict[str, Any]], history: list[tuple[datetime, bool]] | None
) -> str:
    """Tell continuous recording from clips, from replays and the NVR's history."""
    timed = [probe["delta_s"] for probe in probes if probe.get("delta_s") is not None]
    if any(delta > JUMP_SECONDS for delta in timed):
        return MODE_CLIPS  # asked for a moment, got a later one: nothing in between
    if timed and len(timed) == len(probes):
        return MODE_CONTINUOUS
    if timed and any(p.get("delta_s") is None and not p.get("error") for p in probes):
        return MODE_CLIPS  # the replay works, yet some moments hold no video
    if history and _toggles(history) >= 2:
        return MODE_CLIPS
    return MODE_UNKNOWN


async def probe_replay(
    uri: str, asked: datetime, username: str, password: str, wait: float = PROBE_WAIT
) -> dict[str, Any]:
    """Replay a recording from a moment and read the time of the first frame.

    The NVR stamps replayed packets with their wall-clock time (ONVIF RTP
    header extension 0xABAC, NTP format).
    """
    result: dict[str, Any] = {"asked": asked.isoformat(), "plays": None, "delta_s": None}
    connection = RtspConnection(uri, username, password)
    stamps: list[datetime] = []
    found = asyncio.Event()

    def on_frame(channel: int, packet: bytes) -> None:
        if stamps or channel != 0 or len(packet) < 12 or not packet[0] & 0x10:
            return
        header = 12 + 4 * (packet[0] & 0x0F)
        if packet[header : header + 2] == b"\xab\xac" and len(packet) >= header + 12:
            seconds = int.from_bytes(packet[header + 4 : header + 8], "big")
            fraction = int.from_bytes(packet[header + 8 : header + 12], "big")
            stamps.append(NTP_EPOCH + timedelta(seconds=seconds + fraction / 2**32))
            found.set()

    connection.on_frame = on_frame
    require = {"Require": "onvif-replay"}
    base: str | None = None
    try:
        await connection.connect()
        status, headers, body = await connection.request(
            "DESCRIBE", connection.uri, {"Accept": "application/sdp", **require}
        )
        if status != 200:
            raise RtspError(f"DESCRIBE refused ({status})")
        base = strip_credentials(headers.get("content-base", connection.uri))
        video = next(
            (m for m in parse_sdp(body.decode(errors="replace")) if m.kind == "video"), None
        )
        if video is None:
            raise RtspError("no video track")
        status, _, _ = await connection.request(
            "SETUP",
            media_url(base, video.control),
            {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1", **require},
        )
        if status != 200:
            raise RtspError(f"SETUP refused ({status})")
        stamp = asked.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        status, _, _ = await connection.request(
            "PLAY", base, {"Range": f"clock={stamp}-", **require}
        )
        if status != 200:
            raise RtspError(f"PLAY refused ({status})")
        connection.start_pump()
        with suppress(TimeoutError):
            async with asyncio.timeout(wait):
                await found.wait()
    except RtspError as err:
        result["error"] = str(err)
    finally:
        await connection.close(base)
    if stamps:
        result["plays"] = stamps[0].isoformat()
        result["delta_s"] = round((stamps[0] - asked).total_seconds(), 1)
    return result


@dataclass(eq=False)
class Channel:
    """One camera the NVR records, and what Home Assistant shows of it."""

    group: str
    name: str
    mac: str | None
    detections: Detections
    profiles: dict[str, MediaProfile] = field(default_factory=dict)  # "main", "sub"
    stream_uris: dict[str, str] = field(default_factory=dict)  # without credentials
    sources: set[str] = field(default_factory=set)  # video source tokens in its events
    recordings: dict[str, Recording] = field(default_factory=dict)
    recording: bool | None = None  # the NVR's recording job, None until it tells
    mode: str = MODE_UNKNOWN
    names: dict[str, str] = field(default_factory=dict)  # name per source, for diagnostics
    analysis: dict[str, Any] = field(default_factory=dict)
    last_events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=40))
    last_values: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    listeners: list[Callable[[], None]] = field(default_factory=list)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener when the recording state or mode changes."""
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    @callback
    def notify(self) -> None:
        """Tell the entities."""
        for listener in list(self.listeners):
            listener()


class AjaxNvrHub:
    """The NVR: its cameras, their live streams, events and recording modes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Prepare the client; nothing is contacted yet."""
        self.hass = hass
        self.entry = entry
        data = entry.data
        # The relay reads these two, as it does on the DoorBell's NVR connection.
        self._username, self._password = data[CONF_USERNAME], data[CONF_PASSWORD]
        self.client = AjaxOnvifClient(
            async_get_clientsession(hass),
            data[CONF_HOST],
            data[CONF_PORT],
            self._username,
            self._password,
        )
        self.identity: DeviceIdentity | None = None
        self.mac: str | None = None
        self.device_id: str | None = None  # the NVR's device, which the cameras hang off
        self.channels: dict[str, Channel] = {}
        self.skipped: dict[str, str] = {}  # channel -> why it has no device here
        self.topics: list[str] = []
        self.jobs: dict[str, str] = {}  # recording job token -> channel
        self.unrouted: dict[str, int] = {}  # events no channel claimed, by topic
        self.analyzed: datetime | None = None
        self.events = EventStream(
            hass,
            self.client,
            self._on_event,
            self._on_connection,
            self._async_events_refused,
            self._events_recovered,
        )
        self.port = 0
        self._server: asyncio.Server | None = None
        self._relays: set[asyncio.Task[None]] = set()
        self._replay_uris: dict[str, str] = {}
        self._connection_listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._analysis: asyncio.Task[None] | None = None

    # ----------------------------------------------------------------- setup

    async def async_initialize(self) -> None:
        """Read the NVR's cameras, recordings and topics; open the relay."""
        client = self.client
        await client.async_update_time_offset()
        services = await client.async_get_services()
        if not {"search", "replay"} <= services.keys():
            raise OnvifError(f"{client.host}: no recording services, not an NVR")
        self.identity = await client.async_get_identity()
        with suppress(OnvifError):
            self.mac = await client.async_get_mac()
        profiles = await client.async_get_profiles()
        details: dict[str, ProfileDetails] = {}
        with suppress(OnvifError):
            details = await client.async_get_profile_details()
        doorbells = {
            entry.unique_id
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if not is_nvr_entry(entry) and entry.unique_id
        }
        for profile in sorted(profiles, key=lambda p: -p.pixels):
            group = channel_group(profile.token)
            mac = channel_mac(group)
            if mac is not None and mac in doorbells:
                self.skipped[group] = "doorbell"  # it has its own entry, connected directly
                continue
            channel = self.channels.get(group)
            if channel is None:
                channel = self.channels[group] = Channel(group, "", mac, Detections(self.hass))
            quality = "sub" if is_sub(profile.token) else "main"
            if quality in channel.profiles:
                continue
            channel.profiles[quality] = profile
            if (detail := details.get(profile.token)) is not None:
                channel.sources |= detail.sources
            try:
                channel.stream_uris[quality] = self._aim(
                    await client.async_get_stream_uri(profile.token)
                )
            except OnvifError as err:
                _LOGGER.debug("%s: no stream for %s: %s", client.host, profile.token, err)
        for recording in await client.async_find_recordings():
            if (channel := self.channels.get(channel_group(recording.token))) is not None:
                channel.recordings.setdefault(
                    "sub" if is_sub(recording.token) else "main", recording
                )
        for channel in self.channels.values():
            main = channel.profiles.get("main") or next(iter(channel.profiles.values()))
            detail = details.get(main.token)
            recording = channel.recordings.get("main") or next(
                iter(channel.recordings.values()), None
            )
            channel.names = {
                "profile": channel_name(detail.name) if detail else "",
                "recording": recording.source if recording else "",
                "video_source": channel_name(detail.source_name) if detail else "",
                "fallback": channel_name(main.name),
            }
        for group, name in pick_names({g: c.names for g, c in self.channels.items()}).items():
            self.channels[group].name = name
        with suppress(OnvifError):
            jobs = await client.async_get_recording_jobs()
            self.jobs = {job: channel_group(recording) for job, recording in jobs.items()}
        with suppress(OnvifError):
            self.topics = await client.async_get_topics()
        self.events.topics = self.subscription_topics
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        _LOGGER.debug(
            "NVR %s: cameras %s, left out %s",
            client.host,
            {c.name: sorted(c.stream_uris) for c in self.channels.values()},
            self.skipped,
        )

    def start(self, entry: ConfigEntry) -> None:
        """Listen to the cameras and measure how they are recorded."""
        self.events.start(entry)
        self._unsubs.append(async_call_later(self.hass, ANALYZE_DELAY, self._schedule_analysis))
        self._unsubs.append(
            async_track_time_interval(self.hass, self._schedule_analysis, ANALYZE_EVERY)
        )

    async def async_shutdown(self) -> None:
        """Stop everything this hub started."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if (task := self._analysis) is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self.events.async_stop()
        for channel in self.channels.values():
            channel.detections.shutdown()
        if self._server is not None:
            self._server.close()
            self._server = None
        for relay in list(self._relays):
            relay.cancel()
        if self._relays:
            await asyncio.wait(self._relays, timeout=2)

    def _aim(self, uri: str) -> str:
        """Point a URI the NVR announces at the address Home Assistant uses."""
        parts = urlsplit(strip_credentials(uri))
        netloc = host_port(self.client.host, parts.port or DEFAULT_RTSP_PORT)
        return urlunsplit(parts._replace(netloc=netloc))

    @property
    def subscription_topics(self) -> list[str]:
        """Return the advertised topics the cameras' entities use."""
        standard_objects = any(_matches(t, TOPIC_OBJECT[:1]) for t in self.topics)
        used = TOPIC_MOTION + TOPIC_PIR + TOPIC_OBJECT + TOPIC_JOB
        return [
            topic
            for topic in self.topics
            if _matches(topic, used) and not (standard_objects and _matches(topic, TOPIC_TRACKING))
        ]

    # --------------------------------------------------------------- devices

    @property
    def unique_base(self) -> str:
        """Return the stable id devices and entities are built on."""
        return self.entry.unique_id or self.entry.entry_id

    @property
    def device_info(self) -> DeviceInfo:
        """Return the NVR's device."""
        identity = self.identity
        info = DeviceInfo(
            identifiers={(DOMAIN, self.unique_base)},
            manufacturer=MANUFACTURER,
            model="NVR",
            model_id=identity.model if identity else None,
            name=self.entry.title,
            serial_number=identity.serial if identity else None,
            sw_version=identity.firmware if identity else None,
        )
        if self.mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, dr.format_mac(self.mac))}
        return info

    def channel_device_info(self, channel: Channel) -> DeviceInfo:
        """Return a camera's device, attached to the NVR."""
        info = DeviceInfo(
            identifiers={(DOMAIN, f"{self.unique_base}_{channel.group}")},
            manufacturer=MANUFACTURER,
            model="Camera",
            name=channel.name,
        )
        if self.device_id:
            info["via_device_id"] = self.device_id
        if channel.mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, channel.mac)}
        return info

    # ------------------------------------------------------------------ live

    def live_url(self, channel: Channel, quality: str = "main") -> str | None:
        """Return the local relay URL of a camera's live stream."""
        if quality not in channel.stream_uris:
            quality = "main"
        if quality not in channel.stream_uris or not self.port:
            return None
        return f"rtsp://127.0.0.1:{self.port}/live/{quote(channel.group, safe='')}/{quality}"

    async def async_snapshot(
        self, camera: Camera, width: int | None, height: int | None
    ) -> bytes | None:
        """Return a frame from go2rtc (the NVR has no snapshot URI)."""
        provider = camera.webrtc_provider
        if provider is not None and hasattr(provider, "async_get_image"):
            try:
                return await provider.async_get_image(camera, width, height)
            except Exception as err:
                _LOGGER.debug("go2rtc could not grab a frame: %s", err)
        return None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._relays.add(task)
        relay = _LiveRelay(self, reader, writer)  # type: ignore[arg-type]
        try:
            await relay.run()
        except (OSError, asyncio.IncompleteReadError, ValueError, RtspError, TimeoutError) as err:
            _LOGGER.debug("Live relay closed: %s", err or type(err).__name__)
        finally:
            await relay.close()
            self._relays.discard(task)

    # ---------------------------------------------------------------- events

    @callback
    def async_add_connection_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener when the event stream connects or drops."""
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    @callback
    def _on_connection(self, connected: bool) -> None:
        for listener in list(self._connection_listeners):
            listener()

    async def _async_events_refused(self) -> None:
        self.entry.async_start_reauth(self.hass)

    @callback
    def _events_recovered(self) -> None:
        """Nothing to undo: the reauth flow ends by itself."""

    def _route(self, source: dict[str, str]) -> Channel | None:
        """Find the camera an event is about, from its source items."""
        for key, value in source.items():
            if key == "Rule" or not value:
                continue
            for channel in self.channels.values():
                if value in channel.sources:
                    return channel
            if (group := self.jobs.get(value)) is not None:
                return self.channels.get(group)
            for group, channel in self.channels.items():
                if group in value:
                    return channel
        return None

    @callback
    def _on_event(self, event: OnvifEvent) -> None:
        topic = normalize_topic(event.topic)
        data = event.data
        if _matches(topic, TOPIC_TRACKING) and _value(data, "ClassTypes", "ClassType") is None:
            return  # live object tracking: boxes, several times a second
        channel = self._route(event.source)
        if channel is None:
            if not any(group in value for group in self.skipped for value in event.source.values()):
                self.unrouted[topic] = self.unrouted.get(topic, 0) + 1
            return
        # One source per topic and rule: Ajax devices repeat each one's state.
        source = "|".join((topic, *(f"{k}={v}" for k, v in sorted(event.source.items()))))
        values = tuple(sorted(data.items()))
        if channel.last_values.get(source) != values:
            channel.last_values[source] = values
            channel.last_events.append(
                {
                    "received": dt_util.utcnow().isoformat(),
                    "sent": event.utc_time.isoformat() if event.utc_time else None,
                    "topic": event.topic,
                    "operation": event.operation,
                    "data": data,
                }
            )
        if _matches(topic, TOPIC_JOB):
            state = (_value(data, "State") or "").strip().lower()
            recording = state in ("active", "partiallyactive")
            if recording != channel.recording:
                channel.recording = recording
                channel.notify()
        elif _matches(topic, TOPIC_PIR):
            channel.detections.set("pir", _is_true(_value(data, "Detected", "State")), source)
        elif _matches(topic, TOPIC_MOTION):
            state = _value(data, "State", "IsMotion", "Detected")
            channel.detections.set("motion", _is_true(state), source)
        elif _matches(topic, TOPIC_OBJECT):
            raw = _value(data, "ClassTypes", "ClassType") or ""
            found = {_OBJECT_CLASSES.get(c.strip().lower()) for c in raw.split(",")}
            for key in ("person", "vehicle", "pet"):
                channel.detections.set(key, key in found, source)

    # ------------------------------------------------------- recording modes

    @callback
    def _schedule_analysis(self, _now: datetime | None = None) -> None:
        if self._analysis is None or self._analysis.done():
            self._analysis = self.entry.async_create_background_task(
                self.hass, self.async_analyze(), f"{DOMAIN} NVR recordings {self.client.host}"
            )

    async def _async_replay_uri(self, recording: str) -> str:
        if recording not in self._replay_uris:
            self._replay_uris[recording] = self._aim(
                await self.client.async_get_replay_uri(recording)
            )
        return self._replay_uris[recording]

    async def async_analyze(self) -> None:
        """Measure how each camera is recorded: continuously, or clips around events."""
        now = dt_util.utcnow() + self.client.time_offset  # the NVR's clock
        for channel in list(self.channels.values()):
            recording = channel.recordings.get("main") or next(
                iter(channel.recordings.values()), None
            )
            if recording is None:
                channel.mode, channel.analysis = MODE_NOT_RECORDED, {"at": now.isoformat()}
                channel.notify()
                continue
            analysis: dict[str, Any] = {"at": now.isoformat()}
            history: list[tuple[datetime, bool]] | None = None
            try:
                history = await self.client.async_find_recording_history(
                    recording.token, now - HISTORY_SPAN, now
                )
                analysis["history"] = [(t.isoformat(), on) for t, on in history[-20:]]
            except OnvifError as err:
                analysis["history_error"] = str(err)
            probes: list[dict[str, Any]] = []
            try:
                uri = await self._async_replay_uri(recording.token)
            except OnvifError as err:
                analysis["replay_error"] = str(err)
            else:
                for minutes in PROBE_MINUTES:
                    asked = now - timedelta(minutes=minutes)
                    if recording.earliest and asked < recording.earliest + timedelta(minutes=1):
                        continue  # before the recording began
                    probes.append(await probe_replay(uri, asked, self._username, self._password))
            analysis["probes"] = probes
            channel.mode = recording_mode(probes, history)
            channel.analysis = analysis
            channel.notify()
        self.analyzed = dt_util.utcnow()

    # ----------------------------------------------------------- diagnostics

    def as_diagnostics(self) -> dict[str, Any]:
        """Describe the NVR and its cameras, without MAC addresses."""
        macs = {c.mac.replace(":", "") for c in self.channels.values() if c.mac}
        macs |= {mac.replace(":", "") for mac in (channel_mac(g) for g in self.skipped) if mac}

        def hide(value: Any) -> Any:
            if isinstance(value, str):
                for mac in macs:
                    value = value.replace(mac, "**MAC**")
                return value
            if isinstance(value, dict):
                return {hide(k): hide(v) for k, v in value.items()}
            if isinstance(value, (list, tuple, set)):
                return [hide(v) for v in value]
            return value

        return hide(
            {
                "model": self.identity.model if self.identity else None,
                "firmware": self.identity.firmware if self.identity else None,
                "services": sorted(self.client.services),
                "clock_offset_s": round(self.client.time_offset.total_seconds(), 1),
                "left_out": self.skipped,
                "topics": self.topics,
                "event_stream": {
                    "connected": self.events.connected,
                    "filtered": self.events.filtered,
                    "topics": self.events.topics,
                    "last_error": self.events.last_error,
                },
                "unrouted_events": self.unrouted,
                "recording_jobs": self.jobs,
                "analyzed": self.analyzed.isoformat() if self.analyzed else None,
                "cameras": [
                    {
                        "name": channel.name,
                        "names": channel.names,
                        "group": channel.group,
                        "profile_tokens": {q: p.token for q, p in channel.profiles.items()},
                        "profiles": {
                            quality: {
                                "encoding": p.encoding,
                                "size": f"{p.width}x{p.height}",
                                "fps": p.fps,
                                "audio": p.audio_encoding,
                            }
                            for quality, p in channel.profiles.items()
                        },
                        "streams": {q: urlsplit(u).path for q, u in channel.stream_uris.items()},
                        "video_sources": sorted(channel.sources),
                        "recordings": {
                            quality: {
                                "token": r.token,
                                "earliest": r.earliest.isoformat() if r.earliest else None,
                                "latest": r.latest.isoformat() if r.latest else None,
                            }
                            for quality, r in channel.recordings.items()
                        },
                        "recording_now": channel.recording,
                        "mode": channel.mode,
                        "analysis": channel.analysis,
                        "detections": channel.detections.state,
                        "last_events": list(channel.last_events),
                    }
                    for channel in self.channels.values()
                ],
            }
        )


class _LiveRelay(_ReplayRelay):
    """One client (go2rtc) of a camera's live stream, relayed to the NVR."""

    async def _answer(self, line: str, headers: dict[str, str], body: bytes) -> None:
        hub: AjaxNvrHub = self._nvr  # type: ignore[assignment]
        method, url = ([*line.split(" "), "", ""])[:2]
        cseq = headers.get("cseq", "0")
        parts = urlsplit(url)
        match = _LIVE_PATH.match(parts.path)
        channel = hub.channels.get(unquote(match.group(1))) if match else None
        if match is None or channel is None or match.group(2) not in channel.stream_uris:
            self._reply(cseq, "404 Not Found")
            return
        name, quality, suffix = match.groups()
        local = f"{parts.scheme}://{parts.netloc}/live/{name}/{quality}"
        upstream = channel.stream_uris[quality].rstrip("/")
        if method == "SETUP" and "tcp" not in headers.get("transport", "").lower():
            self._reply(cseq, "461 Unsupported Transport")  # clients retry over TCP
            return
        # Without go2rtc's backchannel requirement the NVR offers a plain stream:
        # its tracks are marked sendrecv, which the SDP rewrite turns into recvonly.
        out = {
            key: value
            for key, value in headers.items()
            if key not in ("cseq", "require", "authorization", "content-length")
        }
        status, reply, reply_body = await self._forward(
            method, upstream + (suffix or ""), out, body
        )
        reply = {key: value.replace(upstream, local) for key, value in reply.items()}
        if method == "DESCRIBE" and status.startswith("200"):
            reply_body = rewrite_sdp(reply_body, upstream, local)
        self._reply(cseq, status, reply, reply_body)
        self._released.set()


async def async_setup_nvr_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Connect to the NVR and add its cameras."""
    hub = AjaxNvrHub(hass, entry)
    try:
        await hub.async_initialize()
    except OnvifAuthError as err:
        await hub.async_shutdown()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except OnvifError as err:
        await hub.async_shutdown()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err
    entry.runtime_data = hub
    # The NVR's device first, so the cameras can name it as theirs (via_device_id).
    hub.device_id = (
        dr.async_get(hass)
        .async_get_or_create(config_entry_id=entry.entry_id, **dict(hub.device_info))
        .id
    )
    await hass.config_entries.async_forward_entry_setups(entry, NVR_PLATFORMS)
    hub.start(entry)
    return True


async def async_unload_nvr_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove the cameras and stop listening to the NVR."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, NVR_PLATFORMS)
    if unloaded and (hub := getattr(entry, "runtime_data", None)) is not None:
        await hub.async_shutdown()
    return unloaded
