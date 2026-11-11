"""Small async ONVIF client for Ajax video devices.

It covers what the integration needs and nothing more: device identity, media
profiles and stream URIs, the event topic set and a PullPoint subscription.
Requests are SOAP 1.2 with a WS-Security UsernameToken digest; when the device
answers 401 the client switches to HTTP Digest as well and keeps it.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import logging
import secrets
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import aiohttp

_LOGGER = logging.getLogger(__name__)

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_WSA = "http://www.w3.org/2005/08/addressing"
NS_TT = "http://www.onvif.org/ver10/schema"
NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"
NS_EVENTS = "http://www.onvif.org/ver10/events/wsdl"
NS_WSNT = "http://docs.oasis-open.org/wsn/b-2"
NS_SEARCH = "http://www.onvif.org/ver10/search/wsdl"
NS_REPLAY = "http://www.onvif.org/ver10/replay/wsdl"
NS_RECORDING = "http://www.onvif.org/ver10/recording/wsdl"

SERVICES = {
    NS_DEVICE: "device",
    NS_MEDIA: "media",
    NS_MEDIA2: "media2",
    NS_EVENTS: "events",
    "http://www.onvif.org/ver10/deviceIO/wsdl": "deviceio",
    "http://www.onvif.org/ver10/search/wsdl": "search",
    "http://www.onvif.org/ver10/replay/wsdl": "replay",
    "http://www.onvif.org/ver10/recording/wsdl": "recording",
}
# Paths Ajax uses when GetServices has not been read yet.
DEFAULT_PATHS = {
    "device": "device_service",
    "media": "media_service",
    "media2": "media2_service",
    "events": "event_service",
}
TOPIC_PREFIXES = {
    "http://www.onvif.org/ver10/topics": "tns1",
    "http://ajax.systems/onvif/topics": "tnsajax",
}
TOPIC_DIALECT = "http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet"

ACTION_PULL = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest"
ACTION_SYNC = (
    "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/SetSynchronizationPointRequest"
)
ACTION_RENEW = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest"
ACTION_UNSUBSCRIBE = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"


class OnvifError(Exception):
    """The device answered with an error."""


class OnvifConnectionError(OnvifError):
    """The device could not be reached."""


class OnvifAuthError(OnvifError):
    """The device refused the credentials."""


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """What GetDeviceInformation returns."""

    manufacturer: str
    model: str
    firmware: str
    serial: str


@dataclass(frozen=True, slots=True)
class MediaProfile:
    """One media profile, reduced to what the integration shows or uses."""

    token: str
    name: str
    encoding: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate: int | None = None
    gov_length: int | None = None
    audio_encoding: str | None = None
    audio_sample_rate: int | None = None
    audio_output: bool = False

    @property
    def pixels(self) -> int:
        """Return the frame size, used to rank main and sub streams."""
        return (self.width or 0) * (self.height or 0)


@dataclass(frozen=True, slots=True)
class OnvifEvent:
    """One notification from a PullPoint."""

    topic: str
    operation: str
    utc_time: datetime | None
    source: dict[str, str]
    data: dict[str, str]


@dataclass(slots=True)
class PullPoint:
    """An active PullPoint subscription."""

    address: str
    # WS-Addressing reference parameters the device wants echoed in every
    # request to the subscription, as serialized header blocks.
    reference_parameters: str = ""


@dataclass(frozen=True, slots=True)
class Recording:
    """One recording of an NVR (Profile G): a camera stream kept on its disk."""

    token: str
    source: str  # the camera name the NVR shows
    earliest: datetime | None
    latest: datetime | None


def _parse_time(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse_recordings(roots: list[ET.Element]) -> list[Recording]:
    """Parse GetRecordingSearchResults answers."""
    recordings = []
    for root in roots:
        for info in find_all(root, "RecordingInformation"):
            source = find(info, "Source")
            recordings.append(
                Recording(
                    token=find_text(info, "RecordingToken"),
                    source=find_text(source, "Name") if source is not None else "",
                    earliest=_parse_time(find_text(info, "EarliestRecording")),
                    latest=_parse_time(find_text(info, "LatestRecording")),
                )
            )
    return recordings


def _onvif_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_port(host: str, port: int | None) -> str:
    """Return host:port for a URL, with IPv6 literals in brackets."""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}" if port else host


def local_name(tag: str) -> str:
    """Strip the namespace from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def find_all(elem: ET.Element, name: str) -> list[ET.Element]:
    """Return the descendants of elem (not elem itself) with this local name."""
    return [n for n in elem.iter() if n is not elem and local_name(n.tag) == name]


def find(elem: ET.Element | None, name: str) -> ET.Element | None:
    """Return the first descendant with this local name."""
    if elem is None:
        return None
    return next((n for n in elem.iter() if n is not elem and local_name(n.tag) == name), None)


def find_any(elem: ET.Element | None, *names: str) -> ET.Element | None:
    """Return the first descendant matching any of the names, in name order."""
    for name in names:
        if (node := find(elem, name)) is not None:
            return node
    return None


def find_text(elem: ET.Element | None, name: str) -> str:
    """Return the stripped text of the first descendant with this local name."""
    node = find(elem, name)
    return (node.text or "").strip() if node is not None else ""


def _int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def parse_fault(raw: bytes) -> str:
    """Return a short readable reason from a SOAP fault body."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return raw[:120].decode(errors="replace") or "empty response"
    codes = [n.text for n in find_all(root, "Value") if n.text]
    return " ".join(filter(None, [find_text(root, "Text"), *codes])) or "SOAP fault"


def parse_profiles(root: ET.Element) -> list[MediaProfile]:
    """Parse a Media2 (or Media1) GetProfiles response."""
    profiles = []
    for node in find_all(root, "Profiles"):
        video = find_any(node, "VideoEncoder", "VideoEncoderConfiguration")
        audio = find_any(node, "AudioEncoder", "AudioEncoderConfiguration")
        resolution = find(video, "Resolution")
        rate = find(video, "RateControl")
        gov = (video.get("GovLength") if video is not None else None) or find_text(
            video, "GovLength"
        )
        profiles.append(
            MediaProfile(
                token=node.get("token", ""),
                name=find_text(node, "Name"),
                encoding=find_text(video, "Encoding") or None,
                width=_int(find_text(resolution, "Width")),
                height=_int(find_text(resolution, "Height")),
                fps=_float(find_text(rate, "FrameRateLimit")),
                bitrate=_int(find_text(rate, "BitrateLimit")),
                gov_length=_int(gov),
                audio_encoding=find_text(audio, "Encoding") or None,
                audio_sample_rate=_int(find_text(audio, "SampleRate")),
                audio_output=(
                    find_any(node, "AudioOutput", "AudioOutputConfiguration") is not None
                ),
            )
        )
    return [p for p in profiles if p.token]


def parse_topics(root: ET.Element) -> list[str]:
    """Flatten a GetEventProperties TopicSet into topic paths."""
    topic_set = find(root, "TopicSet")
    topics: list[str] = []
    if topic_set is None:
        return topics

    def walk(node: ET.Element, path: list[str]) -> None:
        for child in node:
            name = local_name(child.tag)
            if name in ("MessageDescription", "Documentation"):
                continue
            prefix = TOPIC_PREFIXES.get(_namespace(child.tag))
            if not path:
                part = f"{prefix or 'tns1'}:{name}"
            elif prefix and prefix != "tns1":
                part = f"{prefix}:{name}"
            else:
                part = name
            if any(
                local_name(key) == "topic" and value.lower() == "true"
                for key, value in child.attrib.items()
            ):
                topics.append("/".join([*path, part]))
            walk(child, [*path, part])

    walk(topic_set, [])
    return topics


def parse_notifications(root: ET.Element) -> list[OnvifEvent]:
    """Parse the NotificationMessage elements of a PullMessages response."""
    events = []
    for msg in find_all(root, "NotificationMessage"):
        topic = find_text(msg, "Topic")
        message = find(find(msg, "Message"), "Message")
        if message is None:
            continue
        items: dict[str, dict[str, str]] = {"Source": {}, "Data": {}}
        for block, values in items.items():
            node = find(message, block)
            if node is None:
                continue
            for item in find_all(node, "SimpleItem"):
                if (name := item.get("Name")) is not None:
                    values[name] = item.get("Value", "")
        utc_time = None
        if stamp := message.get("UtcTime"):
            try:
                utc_time = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                utc_time = None
            if utc_time is not None and utc_time.tzinfo is None:
                utc_time = utc_time.replace(tzinfo=UTC)  # ONVIF times are UTC
        events.append(
            OnvifEvent(
                topic=topic,
                operation=message.get("PropertyOperation", ""),
                utc_time=utc_time,
                source=items["Source"],
                data=items["Data"],
            )
        )
    return events


class AjaxOnvifClient:
    """Talk ONVIF to one Ajax DoorBell, camera or NVR."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.host = host
        self.port = port
        self._username = username
        self._password = password
        self._offset = timedelta(0)
        self._http_digest = False
        self._digest_middleware: aiohttp.DigestAuthMiddleware | None = None
        # aiohttp binds a Digest middleware to the origin it first talks to,
        # so snapshots (often on another port) get their own, per origin.
        self._snapshot_middlewares: dict[str, aiohttp.DigestAuthMiddleware] = {}
        self.services: dict[str, str] = {}

    def _new_digest(self) -> aiohttp.DigestAuthMiddleware:
        try:
            return aiohttp.DigestAuthMiddleware(self._username, self._password)
        except ValueError as err:  # e.g. a ':' in the username
            raise OnvifAuthError(f"{self.host}: {err}") from err

    @property
    def _digest(self) -> aiohttp.DigestAuthMiddleware:
        """Return the HTTP Digest middleware for the ONVIF port, built on first use."""
        if self._digest_middleware is None:
            self._digest_middleware = self._new_digest()
        return self._digest_middleware

    @property
    def time_offset(self) -> timedelta:
        """Return how far the device clock is ahead of ours."""
        return self._offset

    @property
    def uses_http_digest(self) -> bool:
        """Return True once the device asked for HTTP Digest."""
        return self._http_digest

    def _same_host(self, url: str) -> str:
        """Point a device-provided address at the host we were given.

        Devices report their own idea of their address; after a DHCP change
        that is not the one Home Assistant reaches them on. The port the device
        gives is kept.
        """
        parts = urlsplit(url)
        if not parts.scheme.startswith("http") or not parts.hostname:
            return url
        return urlunsplit(parts._replace(netloc=host_port(self.host, parts.port)))

    def service_url(self, name: str) -> str:
        """Return the endpoint of a service."""
        if name in self.services:
            return self.services[name]
        return f"http://{host_port(self.host, self.port)}/onvif/{DEFAULT_PATHS[name]}"

    def _security_header(self) -> str:
        created = (datetime.now(UTC) + self._offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        nonce = secrets.token_bytes(16)
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self._password.encode()).digest()
        ).decode()
        return (
            '<wsse:Security s:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org'
            '/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http:'
            "//docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0"
            '.xsd"><wsse:UsernameToken>'
            f"<wsse:Username>{escape(self._username)}</wsse:Username>"
            '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-'
            f'wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
            '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-'
            '200401-wss-soap-message-security-1.0#Base64Binary">'
            f"{base64.b64encode(nonce).decode()}</wsse:Nonce>"
            f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
        )

    async def _call(
        self,
        url: str,
        body: str,
        *,
        auth: bool = True,
        action: str | None = None,
        timeout: float = 10,
        extra_header: str = "",
    ) -> ET.Element:
        header = self._security_header() if auth else ""
        if action:
            header += f"<wsa:Action>{action}</wsa:Action><wsa:To>{escape(url)}</wsa:To>"
        header += extra_header
        envelope = (
            f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{NS_SOAP}" '
            f'xmlns:wsa="{NS_WSA}" xmlns:tt="{NS_TT}">'
            + (f"<s:Header>{header}</s:Header>" if header else "")
            + f"<s:Body>{body}</s:Body></s:Envelope>"
        )
        content_type = "application/soap+xml; charset=utf-8"
        if action:
            content_type += f'; action="{action}"'
        try:
            async with self._session.post(
                url,
                data=envelope.encode(),
                headers={"Content-Type": content_type},
                timeout=aiohttp.ClientTimeout(total=timeout),
                # Per-request middlewares replace the session's, including Home
                # Assistant's redirect guard: never follow a redirect instead.
                middlewares=(self._digest,) if auth and self._http_digest else (),
                allow_redirects=False,
            ) as resp:
                status, raw = resp.status, await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OnvifConnectionError(f"{self.host}: {str(err) or type(err).__name__}") from err
        refused = status == 401 or (status in (400, 500) and b"NotAuthorized" in raw)
        if refused:
            if auth and not self._http_digest:
                _LOGGER.debug("%s asks for HTTP Digest, switching", self.host)
                self._http_digest = True
                return await self._call(
                    url,
                    body,
                    auth=auth,
                    action=action,
                    timeout=timeout,
                    extra_header=extra_header,
                )
            raise OnvifAuthError(f"{self.host}: credentials refused")
        if status >= 400:
            raise OnvifError(f"{self.host}: HTTP {status} {parse_fault(raw)}")
        try:
            return ET.fromstring(raw)
        except ET.ParseError as err:
            raise OnvifError(f"{self.host}: invalid XML ({err})") from err

    async def async_update_time_offset(self) -> timedelta:
        """Read the device clock; WS-Security tokens are checked against it."""
        root = await self._call(
            self.service_url("device"),
            f'<GetSystemDateAndTime xmlns="{NS_DEVICE}"/>',
            auth=False,
        )
        utc = find(root, "UTCDateTime")
        if utc is not None:
            date, time = find(utc, "Date"), find(utc, "Time")
            try:
                device = datetime(
                    int(find_text(date, "Year")),
                    int(find_text(date, "Month")),
                    int(find_text(date, "Day")),
                    int(find_text(time, "Hour")),
                    int(find_text(time, "Minute")),
                    int(find_text(time, "Second")),
                    tzinfo=UTC,
                )
            except ValueError:
                return self._offset
            self._offset = device - datetime.now(UTC)
        return self._offset

    async def async_get_services(self, *, auth: bool = True) -> dict[str, str]:
        """Read the service endpoints (Ajax devices also answer without an account)."""
        root = await self._call(
            self.service_url("device"),
            f'<GetServices xmlns="{NS_DEVICE}"><IncludeCapability>false'
            "</IncludeCapability></GetServices>",
            auth=auth,
        )
        for svc in find_all(root, "Service"):
            name = SERVICES.get(find_text(svc, "Namespace"))
            if name and (xaddr := find_text(svc, "XAddr")):
                self.services[name] = self._same_host(xaddr)
        return self.services

    async def async_get_identity(self) -> DeviceIdentity:
        """Read manufacturer, model, firmware and serial number."""
        root = await self._call(
            self.service_url("device"), f'<GetDeviceInformation xmlns="{NS_DEVICE}"/>'
        )
        return DeviceIdentity(
            manufacturer=find_text(root, "Manufacturer"),
            model=find_text(root, "Model"),
            firmware=find_text(root, "FirmwareVersion"),
            serial=find_text(root, "SerialNumber"),
        )

    async def async_get_mac(self) -> str | None:
        """Read the MAC address of the first network interface."""
        root = await self._call(
            self.service_url("device"), f'<GetNetworkInterfaces xmlns="{NS_DEVICE}"/>'
        )
        return find_text(root, "HwAddress").lower() or None

    async def async_get_profiles(self) -> list[MediaProfile]:
        """Read the media profiles, with Media2 first and Media1 as a fallback."""
        try:
            root = await self._call(
                self.service_url("media2"),
                f'<GetProfiles xmlns="{NS_MEDIA2}"><Type>All</Type></GetProfiles>',
            )
        except OnvifAuthError:
            raise
        except OnvifError as err:
            _LOGGER.debug("%s: Media2 GetProfiles failed (%s), trying Media1", self.host, err)
            root = await self._call(self.service_url("media"), f'<GetProfiles xmlns="{NS_MEDIA}"/>')
        return parse_profiles(root)

    async def async_get_stream_uri(self, token: str) -> str:
        """Return the RTSP URI of a profile."""
        try:
            root = await self._call(
                self.service_url("media2"),
                f'<GetStreamUri xmlns="{NS_MEDIA2}"><Protocol>RTSP</Protocol>'
                f"<ProfileToken>{escape(token)}</ProfileToken></GetStreamUri>",
            )
        except OnvifAuthError:
            raise
        except OnvifError:
            root = await self._call(
                self.service_url("media"),
                f'<GetStreamUri xmlns="{NS_MEDIA}"><StreamSetup><tt:Stream>RTP-Unicast'
                "</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>"
                "</tt:Transport></StreamSetup>"
                f"<ProfileToken>{escape(token)}</ProfileToken></GetStreamUri>",
            )
        if not (uri := find_text(root, "Uri")):
            raise OnvifError(f"{self.host}: no stream URI for profile {token}")
        return uri

    async def async_get_snapshot_uri(self, token: str) -> str | None:
        """Return the JPEG snapshot URI of a profile, if the device has one."""
        try:
            root = await self._call(
                self.service_url("media2"),
                f'<GetSnapshotUri xmlns="{NS_MEDIA2}">'
                f"<ProfileToken>{escape(token)}</ProfileToken></GetSnapshotUri>",
            )
        except OnvifError:  # optional: a refusal here must not block the setup
            return None
        return find_text(root, "Uri") or None

    async def async_find_recordings(self) -> list[Recording]:
        """List the recordings of an NVR (search service, Profile G)."""
        url = self.service_url("search")
        found = await self._call(
            url,
            f'<FindRecordings xmlns="{NS_SEARCH}"><Scope/><MaxMatches>64</MaxMatches>'
            "<KeepAliveTime>PT10S</KeepAliveTime></FindRecordings>",
        )
        token = escape(find_text(found, "SearchToken"))
        roots = []
        try:
            for _ in range(10):
                root = await self._call(
                    url,
                    f'<GetRecordingSearchResults xmlns="{NS_SEARCH}"><SearchToken>{token}'
                    "</SearchToken><MinResults>1</MinResults><MaxResults>64</MaxResults>"
                    "<WaitTime>PT5S</WaitTime></GetRecordingSearchResults>",
                )
                roots.append(root)
                if find_text(root, "SearchState") in ("Completed", ""):
                    break
        finally:
            with suppress(OnvifError):  # searches also expire by themselves
                await self._call(
                    url,
                    f'<EndSearch xmlns="{NS_SEARCH}"><SearchToken>{token}</SearchToken></EndSearch>',
                )
        return parse_recordings(roots)

    async def async_get_replay_uri(self, recording: str) -> str:
        """Return the RTSP URI that replays a recording (Require: onvif-replay)."""
        root = await self._call(
            self.service_url("replay"),
            f'<GetReplayUri xmlns="{NS_REPLAY}"><StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>'
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></StreamSetup>"
            f"<RecordingToken>{escape(recording)}</RecordingToken></GetReplayUri>",
        )
        if not (uri := find_text(root, "Uri")):
            raise OnvifError(f"{self.host}: no replay URI for {recording}")
        return uri

    async def async_get_video_sources(self) -> dict[str, set[str]]:
        """Return the video source tokens of each Media2 profile.

        Events name their camera by one of these (VideoSourceToken).
        """
        root = await self._call(
            self.service_url("media2"),
            f'<GetProfiles xmlns="{NS_MEDIA2}"><Type>All</Type></GetProfiles>',
        )
        sources: dict[str, set[str]] = {}
        for node in find_all(root, "Profiles"):
            source = find_any(node, "VideoSource", "VideoSourceConfiguration")
            tokens = (
                {source.get("token", ""), find_text(source, "SourceToken")}
                if source is not None
                else set()
            )
            sources[node.get("token", "")] = {token for token in tokens if token}
        return sources

    async def async_get_recording_jobs(self) -> dict[str, str]:
        """Return the NVR's recording jobs: job token -> recording token."""
        root = await self._call(
            self.service_url("recording"), f'<GetRecordingJobs xmlns="{NS_RECORDING}"/>'
        )
        return {
            find_text(job, "JobToken"): find_text(job, "RecordingToken")
            for job in find_all(root, "JobItem")
            if find_text(job, "JobToken")
        }

    async def async_find_recording_history(
        self, recording: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, bool]]:
        """Return when a recording started and stopped holding video (FindEvents).

        Each item is (time, recording); the first one may be the state at the start.
        """
        url = self.service_url("search")
        found = await self._call(
            url,
            f'<FindEvents xmlns="{NS_SEARCH}"><StartPoint>{_onvif_time(start)}</StartPoint>'
            f"<EndPoint>{_onvif_time(end)}</EndPoint><Scope><tt:IncludedRecordings>"
            f"{escape(recording)}</tt:IncludedRecordings></Scope><SearchFilter>"
            f'<wsnt:TopicExpression xmlns:wsnt="{NS_WSNT}" '
            f'xmlns:tns1="http://www.onvif.org/ver10/topics" Dialect="{TOPIC_DIALECT}">'
            "tns1:RecordingHistory//.</wsnt:TopicExpression></SearchFilter>"
            "<IncludeStartState>true</IncludeStartState><MaxMatches>500</MaxMatches>"
            "<KeepAliveTime>PT10S</KeepAliveTime></FindEvents>",
        )
        token = escape(find_text(found, "SearchToken"))
        changes: list[tuple[datetime, bool]] = []
        try:
            for _ in range(10):
                root = await self._call(
                    url,
                    f'<GetEventSearchResults xmlns="{NS_SEARCH}"><SearchToken>{token}'
                    "</SearchToken><MinResults>1</MinResults><MaxResults>100</MaxResults>"
                    "<WaitTime>PT5S</WaitTime></GetEventSearchResults>",
                )
                for result in find_all(root, "Result"):
                    moment = _parse_time(find_text(result, "Time"))
                    data = find(result, "Data")
                    values = {
                        item.get("Name"): item.get("Value", "")
                        for item in find_all(result if data is None else data, "SimpleItem")
                    }
                    state = values.get("IsRecording", values.get("IsDataPresent"))
                    if moment is not None and state is not None:
                        changes.append((moment, state.strip().lower() in ("true", "1")))
                if find_text(root, "SearchState") in ("Completed", ""):
                    break
        finally:
            with suppress(OnvifError):
                await self._call(
                    url,
                    f'<EndSearch xmlns="{NS_SEARCH}"><SearchToken>{token}</SearchToken></EndSearch>',
                )
        return sorted(changes)

    async def async_get_topics(self) -> list[str]:
        """Read the event topics the device can send."""
        root = await self._call(
            self.service_url("events"), f'<GetEventProperties xmlns="{NS_EVENTS}"/>'
        )
        return parse_topics(root)

    async def async_create_pullpoint(self, seconds: int, topics: Sequence[str] = ()) -> PullPoint:
        """Create a PullPoint subscription that lives for this many seconds.

        With topics (as parse_topics spells them), the device only queues those.
        """
        expression = ""
        if topics:
            namespaces = " ".join(
                f'xmlns:{prefix}="{namespace}"' for namespace, prefix in TOPIC_PREFIXES.items()
            )
            expression = (
                f'<Filter><wsnt:TopicExpression xmlns:wsnt="{NS_WSNT}" {namespaces} '
                f'Dialect="{TOPIC_DIALECT}">{escape("|".join(topics))}'
                "</wsnt:TopicExpression></Filter>"
            )
        root = await self._call(
            self.service_url("events"),
            f'<CreatePullPointSubscription xmlns="{NS_EVENTS}">{expression}'
            f"<InitialTerminationTime>PT{seconds}S</InitialTerminationTime>"
            "</CreatePullPointSubscription>",
        )
        reference = find(root, "SubscriptionReference")
        address = find_text(reference, "Address")
        if not address:
            raise OnvifError(f"{self.host}: subscription without an address")
        parameters = find(reference, "ReferenceParameters")
        return PullPoint(
            self._same_host(address),
            "".join(ET.tostring(child, encoding="unicode") for child in parameters)
            if parameters is not None
            else "",
        )

    async def async_set_synchronization_point(self, pullpoint: PullPoint) -> None:
        """Ask the device to resend the current value of every property."""
        await self._call(
            pullpoint.address,
            f'<SetSynchronizationPoint xmlns="{NS_EVENTS}"/>',
            action=ACTION_SYNC,
            extra_header=pullpoint.reference_parameters,
        )

    async def async_pull(
        self, pullpoint: PullPoint, wait: int = 10, limit: int = 64
    ) -> list[OnvifEvent]:
        """Long-poll the PullPoint: return as soon as events arrive, or after wait."""
        root = await self._call(
            pullpoint.address,
            f'<PullMessages xmlns="{NS_EVENTS}"><Timeout>PT{wait}S</Timeout>'
            f"<MessageLimit>{limit}</MessageLimit></PullMessages>",
            action=ACTION_PULL,
            extra_header=pullpoint.reference_parameters,
            timeout=wait + 10,
        )
        return parse_notifications(root)

    async def async_renew(self, pullpoint: PullPoint, seconds: int) -> None:
        """Extend the subscription."""
        await self._call(
            pullpoint.address,
            f'<Renew xmlns="{NS_WSNT}"><TerminationTime>PT{seconds}S</TerminationTime></Renew>',
            action=ACTION_RENEW,
            extra_header=pullpoint.reference_parameters,
        )

    async def async_unsubscribe(self, pullpoint: PullPoint) -> None:
        """End the subscription so the device frees it."""
        await self._call(
            pullpoint.address,
            f'<Unsubscribe xmlns="{NS_WSNT}"/>',
            action=ACTION_UNSUBSCRIBE,
            extra_header=pullpoint.reference_parameters,
            timeout=5,
        )

    async def async_get_bytes(self, url: str) -> bytes:
        """Fetch a URL from the device (snapshots) with HTTP Digest.

        Short timeout: Home Assistant gives a still image 10 s in total, and the
        go2rtc fallback needs some of it.
        """
        target = self._same_host(url)
        parts = urlsplit(target)
        origin = f"{parts.scheme}://{parts.netloc}"
        if (middleware := self._snapshot_middlewares.get(origin)) is None:
            middleware = self._snapshot_middlewares[origin] = self._new_digest()
        try:
            async with self._session.get(
                target,
                timeout=aiohttp.ClientTimeout(total=4),
                middlewares=(middleware,),
                allow_redirects=False,
            ) as resp:
                if resp.status == 401:
                    raise OnvifAuthError(f"{self.host}: snapshot refused")
                if resp.status >= 400:
                    raise OnvifError(f"{self.host}: snapshot HTTP {resp.status}")
                return await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OnvifConnectionError(f"{self.host}: {str(err) or type(err).__name__}") from err
