#!/usr/bin/env python3
"""Phase 0 probe for an Ajax DoorBell (or Ajax NVR) on the local network.

Read-only, except with --beep, which plays a one-second 1 kHz tone through the
doorbell speaker to prove the talk-back path.

It uses only the Python standard library and talks to the device directly with
the ONVIF account created in the Ajax app (Device > Settings > Service >
Connection via ONVIF). Credentials come from the environment, are sent to the
device only, and are never printed or written to disk. The report (no secrets)
is printed and, with --report, also saved to a file.

    AJAX_ONVIF_USER=... AJAX_ONVIF_PASS=... python3 -I probe_onvif.py --host 192.168.128.31

The run_phase0.sh wrapper asks for the password with hidden input.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout, suppress
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import io
import itertools
import math
import os
import re
import secrets
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_WSA = "http://www.w3.org/2005/08/addressing"
NS_TT = "http://www.onvif.org/ver10/schema"
NS_TOPICS = {
    "http://www.onvif.org/ver10/topics": "tns1",
    "http://ajax.systems/onvif/topics": "tnsajax",
}
SERVICE_NAMES = {
    "http://www.onvif.org/ver10/device/wsdl": "device",
    "http://www.onvif.org/ver10/media/wsdl": "media",
    "http://www.onvif.org/ver20/media/wsdl": "media2",
    "http://www.onvif.org/ver10/events/wsdl": "events",
    "http://www.onvif.org/ver10/deviceIO/wsdl": "deviceIO",
    "http://www.onvif.org/ver20/imaging/wsdl": "imaging",
    "http://www.onvif.org/ver20/analytics/wsdl": "analytics",
    "http://www.onvif.org/ver10/recording/wsdl": "recording",
    "http://www.onvif.org/ver10/search/wsdl": "search",
    "http://www.onvif.org/ver10/replay/wsdl": "replay",
    "http://www.onvif.org/ver10/advancedsecurity/wsdl": "advancedsecurity",
}
ACTION_PULL = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest"
ACTION_UNSUB = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"
REQUIRE_BACKCHANNEL = "www.onvif.org/ver20/backchannel"


# --------------------------------------------------------------------------- XML


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def ns_of(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def iter_local(elem: ET.Element, name: str):
    """Descendants (not the element itself) whose local tag is name."""
    for node in elem.iter():
        if node is not elem and local(node.tag) == name:
            yield node


def first(elem: ET.Element | None, name: str) -> ET.Element | None:
    if elem is None:
        return None
    return next(iter_local(elem, name), None)


def text(elem: ET.Element | None, name: str, default: str = "") -> str:
    node = first(elem, name)
    return (node.text or "").strip() if node is not None and node.text else default


def children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in elem if local(c.tag) == name]


def flatten_topics(topic_set: ET.Element) -> list[tuple[str, list[str], list[str]]]:
    """Return (topic path, source item names, data item names) for each leaf topic."""
    found: list[tuple[str, list[str], list[str]]] = []

    def walk(node: ET.Element, path: list[str]) -> None:
        for child in node:
            name = local(child.tag)
            if name in ("MessageDescription", "Documentation"):
                continue
            prefix = NS_TOPICS.get(ns_of(child.tag))
            if not path:
                part = f"{prefix or 'tns1'}:{name}"
            else:
                part = f"{prefix}:{name}" if prefix and prefix != "tns1" else name
            new_path = [*path, part]
            is_topic = any(
                local(k) == "topic" and v.lower() == "true" for k, v in child.attrib.items()
            )
            if is_topic:
                desc = first(child, "MessageDescription")
                src, data = [], []
                if desc is not None:
                    for block, out in (("Source", src), ("Data", data)):
                        for b in children(desc, block):
                            for item in iter_local(b, "SimpleItemDescription"):
                                out.append(f"{item.get('Name')}:{local(item.get('Type', ''))}")
                found.append(("/".join(new_path), src, data))
            walk(child, new_path)

    walk(topic_set, [])
    return found


# ------------------------------------------------------------------------ ONVIF


class OnvifError(Exception):
    pass


def parse_fault(raw: bytes) -> str:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return raw[:200].decode(errors="replace") or "(empty body)"
    reason = text(root, "Text") or text(root, "Reason")
    sub = [n.text or "" for n in iter_local(root, "Value")]
    return f"{reason} {' / '.join(s for s in sub if s)}".strip() or "SOAP fault"


class Onvif:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 12):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.timeout = timeout
        self.offset = 0.0  # device clock minus local clock, seconds
        self.use_digest = False
        self.xaddr: dict[str, str] = {}
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, f"http://{host}:{port}/", user, password)
        self._digest = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
        self._plain = urllib.request.build_opener()

    def url(self, service: str) -> str:
        return self.xaddr.get(service) or f"http://{self.host}:{self.port}/onvif/{service}_service"

    def _security(self) -> str:
        created = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=self.offset)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        nonce = secrets.token_bytes(16)
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        ).decode()
        return (
            '<wsse:Security s:mustUnderstand="1" '
            'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
            'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f"<wsse:UsernameToken><wsse:Username>{xml_escape(self.user)}</wsse:Username>"
            '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
            '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
            f"{base64.b64encode(nonce).decode()}</wsse:Nonce>"
            f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
        )

    def call(
        self,
        target: str,
        body: str,
        *,
        auth: bool = True,
        action: str | None = None,
        timeout: float | None = None,
    ) -> ET.Element:
        url = target if target.startswith("http") else self.url(target)
        header = self._security() if auth else ""
        if action:
            header += f"<wsa:Action>{action}</wsa:Action><wsa:To>{xml_escape(url)}</wsa:To>"
        envelope = (
            f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{NS_SOAP}" '
            f'xmlns:wsa="{NS_WSA}" xmlns:tt="{NS_TT}">'
            f"{f'<s:Header>{header}</s:Header>' if header else ''}<s:Body>{body}</s:Body></s:Envelope>"
        )
        ctype = "application/soap+xml; charset=utf-8"
        if action:
            ctype += f'; action="{action}"'
        request = urllib.request.Request(
            url, data=envelope.encode(), headers={"Content-Type": ctype}
        )
        opener = self._digest if (self.use_digest and auth) else self._plain
        try:
            with opener.open(request, timeout=timeout or self.timeout) as resp:
                return ET.fromstring(resp.read())
        except urllib.error.HTTPError as err:
            raw = err.read()
            refused = err.code == 401 or (err.code in (400, 500) and b"NotAuthorized" in raw)
            if refused and auth and not self.use_digest:
                self.use_digest = True  # device wants HTTP Digest on top of WS-Security
                return self.call(target, body, auth=auth, action=action, timeout=timeout)
            raise OnvifError(f"HTTP {err.code}: {parse_fault(raw)}") from None
        except (urllib.error.URLError, OSError) as err:
            raise OnvifError(f"network: {err}") from None


# ------------------------------------------------------------------------- RTSP


@dataclass
class RtspResponse:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Media:
    kind: str
    payloads: list[str] = field(default_factory=list)  # "8 PCMA/8000"
    direction: str = "sendrecv"
    control: str = ""
    fmtp: list[str] = field(default_factory=list)


def parse_sdp(sdp: str) -> list[Media]:
    medias: list[Media] = []
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            medias.append(Media(kind=line[2:].split(" ", 1)[0]))
        elif not medias:
            continue
        elif line.startswith("a=rtpmap:"):
            medias[-1].payloads.append(line[9:])
        elif line.startswith("a=fmtp:"):
            medias[-1].fmtp.append(line[7:])
        elif line.startswith("a=control:"):
            medias[-1].control = line[10:]
        elif line[2:] in ("sendrecv", "sendonly", "recvonly", "inactive"):
            medias[-1].direction = line[2:]
    return medias


class Rtsp:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 8):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.cseq = 0
        self.session: str | None = None
        self._digest: tuple[str, str] | None = None  # realm, nonce
        self._buf = b""

    def close(self) -> None:
        with suppress(OSError):
            self.sock.close()

    def _authorization(self, method: str, uri: str) -> str | None:
        if not self._digest:
            return None
        realm, nonce = self._digest
        ha1 = hashlib.md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return (
            f'Digest username="{self.user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{response}"'
        )

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("connection closed")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read_response(self) -> RtspResponse:
        while True:
            head = self._read_exact(1)
            if head == b"$":  # interleaved RTP before the reply, skip it
                _, size = struct.unpack(">BH", self._read_exact(3))
                self._read_exact(size)
                continue
            while b"\r\n\r\n" not in self._buf:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise OSError("connection closed")
                self._buf += chunk
            raw, self._buf = (head + self._buf).split(b"\r\n\r\n", 1)
            lines = raw.decode(errors="replace").split("\r\n")
            parts = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            body = self._read_exact(int(headers.get("content-length", "0") or 0))
            return RtspResponse(int(parts[1]), parts[2] if len(parts) > 2 else "", headers, body)

    def request(self, method: str, uri: str, headers: dict[str, str] | None = None) -> RtspResponse:
        for _ in range(2):
            self.cseq += 1
            lines = [
                f"{method} {uri} RTSP/1.0",
                f"CSeq: {self.cseq}",
                "User-Agent: ajax-doorbell-probe",
            ]
            if self.session:
                lines.append(f"Session: {self.session}")
            if auth := self._authorization(method, uri):
                lines.append(f"Authorization: {auth}")
            lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
            self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            resp = self._read_response()
            challenge = resp.headers.get("www-authenticate", "")
            if resp.status == 401 and challenge.startswith("Digest") and self._digest is None:
                realm = re.search(r'realm="([^"]*)"', challenge)
                nonce = re.search(r'nonce="([^"]*)"', challenge)
                self._digest = (realm.group(1) if realm else "", nonce.group(1) if nonce else "")
                continue
            if "session" in resp.headers and not self.session:
                self.session = resp.headers["session"].split(";", 1)[0]
            return resp
        return resp

    def _fill(self, size: int) -> None:
        while len(self._buf) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("connection closed")
            self._buf += chunk

    def read_interleaved(self, deadline: float):
        """Yield (channel, payload, receive time) until the deadline.

        A timeout never consumes bytes, so the reader cannot lose framing.
        """
        self.sock.settimeout(0.5)
        while time.monotonic() < deadline:
            try:
                self._fill(1)
                if self._buf[:1] != b"$":  # an RTSP reply inside the stream
                    end = self._buf.find(b"\r\n\r\n")
                    if end < 0:
                        self._fill(len(self._buf) + 1)
                        continue
                    size = re.search(rb"(?im)^content-length:\s*(\d+)", self._buf[:end])
                    total = end + 4 + (int(size.group(1)) if size else 0)
                    self._fill(total)
                    self._buf = self._buf[total:]
                    continue
                self._fill(4)
                channel, size = struct.unpack(">BH", self._buf[1:4])
                self._fill(4 + size)
            except TimeoutError:
                continue
            payload, self._buf = self._buf[4 : 4 + size], self._buf[4 + size :]
            yield channel, payload, time.monotonic()


def media_url(base: str, control: str) -> str:
    if not control or control == "*":
        return base
    if control.startswith("rtsp://"):
        return control
    return base.rstrip("/") + "/" + control


# ------------------------------------------------------------------------ audio


_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)
_SEG_UEND = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def linear_to_alaw(pcm: int) -> int:
    """16-bit linear PCM to G.711 A-law (ITU reference algorithm)."""
    pcm >>= 3
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = next((i for i, end in enumerate(_SEG_AEND) if pcm <= end), 8)
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    aval |= (pcm >> 1) & 0x0F if seg < 2 else (pcm >> seg) & 0x0F
    return aval ^ mask


def linear_to_ulaw(pcm: int) -> int:
    """16-bit linear PCM to G.711 mu-law (ITU reference algorithm)."""
    pcm >>= 2
    if pcm < 0:
        pcm = -pcm
        mask = 0x7F
    else:
        mask = 0xFF
    pcm = min(pcm, 8159) + (0x84 >> 2)
    seg = next((i for i, end in enumerate(_SEG_UEND) if pcm <= end), 8)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((pcm >> (seg + 1)) & 0x0F)) ^ mask


def tone(encoder, seconds: float, rate: int = 8000, freq: float = 1000.0) -> bytes:
    count = int(seconds * rate)
    return bytes(encoder(int(9000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(count))


# ----------------------------------------------------------------------- report


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


def hide(uri: str) -> str:
    return re.sub(r"//[^/@]+@", "//***@", uri)


@dataclass
class Findings:
    model: str = "?"
    firmware: str = "?"
    profiles: list[str] = field(default_factory=list)
    stream_uris: list[str] = field(default_factory=list)
    snapshot: str = ""
    audio_codec: str = "?"
    backchannel: str = "not tested"
    ring_topic: str = "not found"
    ring_seen: list[str] = field(default_factory=list)
    gop: str = "?"
    bitrate: str = "?"
    startup: str = "?"
    profile_g: str = "no"
    beep: str = "not run"
    errors: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------ steps


def step_device(cam: Onvif, f: Findings) -> None:
    section("Device")
    root = cam.call(
        "device_service",
        '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
        auth=False,
    )
    utc = first(root, "UTCDateTime")
    if utc is not None:
        d, t = first(utc, "Date"), first(utc, "Time")
        device = dt.datetime(
            int(text(d, "Year")),
            int(text(d, "Month")),
            int(text(d, "Day")),
            int(text(t, "Hour")),
            int(text(t, "Minute")),
            int(text(t, "Second")),
            tzinfo=dt.UTC,
        )
        cam.offset = (device - dt.datetime.now(dt.UTC)).total_seconds()
        print(
            f"device clock offset: {cam.offset:+.1f} s (type {text(root, 'DateTimeType')}, TZ {text(root, 'TZ')})"
        )

    info = cam.call(
        "device_service", '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
    )
    f.model, f.firmware = text(info, "Model"), text(info, "FirmwareVersion")
    print(
        f"{text(info, 'Manufacturer')} {f.model} firmware {f.firmware} serial {text(info, 'SerialNumber')}"
    )
    print(f"authentication: WS-Security digest{' + HTTP Digest' if cam.use_digest else ''}")

    services = cam.call(
        "device_service",
        '<GetServices xmlns="http://www.onvif.org/ver10/device/wsdl"><IncludeCapability>true</IncludeCapability></GetServices>',
    )
    for svc in iter_local(services, "Service"):
        namespace, xaddr = text(svc, "Namespace"), text(svc, "XAddr")
        name = SERVICE_NAMES.get(namespace, namespace)
        cam.xaddr[name] = xaddr
        caps = first(svc, "Capabilities")
        cap_attrs = ""
        if caps is not None and len(caps):
            cap_attrs = " ".join(f"{k}={v}" for k, v in caps[0].attrib.items())[:220]
        print(f"- {name:16} {xaddr}  {cap_attrs}")
    if {"search", "replay"} & set(cam.xaddr):
        f.profile_g = "search/replay services present"

    try:
        scopes = cam.call(
            "device_service", '<GetScopes xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
        )
        for item in iter_local(scopes, "ScopeItem"):
            print(f"  scope {item.text}")
    except OnvifError as err:
        print(f"GetScopes: {err}")
    try:
        nics = cam.call(
            "device_service",
            '<GetNetworkInterfaces xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
        )
        print(f"MAC: {text(nics, 'HwAddress') or '?'}")
    except OnvifError as err:
        print(f"GetNetworkInterfaces: {err}")


def step_media(cam: Onvif, f: Findings) -> None:
    section("Media2 profiles")
    tr2 = 'xmlns="http://www.onvif.org/ver20/media/wsdl"'
    root = cam.call("media2", f"<GetProfiles {tr2}><Type>All</Type></GetProfiles>")
    for prof in iter_local(root, "Profiles"):
        token = prof.get("token", "?")
        name = text(prof, "Name")
        conf = first(prof, "Configurations")
        venc = first(conf, "VideoEncoder")
        aenc = first(conf, "AudioEncoder")
        aout = first(conf, "AudioOutput")
        adec = first(conf, "AudioDecoder")
        line = f"profile {token} ({name})"
        if venc is not None:
            res = first(venc, "Resolution")
            rate = first(venc, "RateControl")
            line += (
                f" | video {venc.get('GovLength') or text(venc, 'GovLength')} GOP, "
                f"{text(venc, 'Encoding')} {text(res, 'Width')}x{text(res, 'Height')} "
                f"{text(rate, 'FrameRateLimit')} fps {text(rate, 'BitrateLimit')} kbps "
                f"profile {venc.get('Profile', text(venc, 'Profile'))}"
            )
        if aenc is not None:
            codec = f"{text(aenc, 'Encoding')} {text(aenc, 'SampleRate')} Hz {text(aenc, 'Bitrate')} kbps"
            line += f" | audio {codec}"
            f.audio_codec = codec
        if aout is not None:
            line += f" | audio OUTPUT {text(aout, 'OutputToken')} level {text(aout, 'OutputLevel')}"
        if adec is not None:
            line += f" | audio DECODER {adec.get('token')}"
        print(line)
        f.profiles.append(line)
        try:
            uri = cam.call(
                "media2",
                f"<GetStreamUri {tr2}><Protocol>RTSP</Protocol><ProfileToken>{token}</ProfileToken></GetStreamUri>",
            )
            stream = text(uri, "Uri")
            f.stream_uris.append(stream)
            print(f"    stream   {hide(stream)}")
        except OnvifError as err:
            print(f"    GetStreamUri: {err}")
        try:
            snap = cam.call(
                "media2",
                f"<GetSnapshotUri {tr2}><ProfileToken>{token}</ProfileToken></GetSnapshotUri>",
            )
            f.snapshot = text(snap, "Uri")
            print(f"    snapshot {hide(f.snapshot)}")
        except OnvifError as err:
            print(f"    GetSnapshotUri: {err}")

    for call in (
        "GetAudioOutputConfigurations",
        "GetAudioDecoderConfigurations",
        "GetAudioDecoderConfigurationOptions",
    ):
        try:
            root = cam.call("media2", f"<{call} {tr2}/>")
            encodings = sorted({e.text for e in iter_local(root, "Encoding") if e.text})
            rates = sorted({e.text for e in iter_local(root, "Items") if e.text})
            tokens = [c.get("token") for c in iter_local(root, "Configurations") if c.get("token")]
            print(
                f"{call}: tokens={tokens} encodings={encodings} {('values=' + ','.join(rates)) if rates else ''}"
            )
        except OnvifError as err:
            print(f"{call}: {err}")

    section("DeviceIO")
    tmd = 'xmlns="http://www.onvif.org/ver10/deviceIO/wsdl"'
    for call in ("GetAudioOutputs", "GetAudioSources", "GetRelayOutputs", "GetDigitalInputs"):
        try:
            root = cam.call("deviceIO", f"<{call} {tmd}/>")
            tokens = [n.get("token") for n in root.iter() if n.get("token")]
            tokens += [n.text for n in iter_local(root, "Token") if n.text]
            print(f"{call}: {tokens}")
        except OnvifError as err:
            print(f"{call}: {err}")


def step_topics(cam: Onvif, f: Findings) -> None:
    section("Event topics")
    tev = 'xmlns="http://www.onvif.org/ver10/events/wsdl"'
    try:
        caps = cam.call("events", f"<GetServiceCapabilities {tev}/>")
        node = first(caps, "Capabilities")
        if node is not None:
            print("capabilities: " + " ".join(f"{k}={v}" for k, v in node.attrib.items()))
    except OnvifError as err:
        print(f"GetServiceCapabilities: {err}")
    root = cam.call("events", f"<GetEventProperties {tev}/>")
    topic_set = first(root, "TopicSet")
    if topic_set is None:
        print("no TopicSet")
        return
    for topic, src, data in flatten_topics(topic_set):
        print(f"- {topic}  source={src} data={data}")
        if "Ring" in topic or "Doorbell" in topic or "Visitor" in topic or "Call" in topic:
            f.ring_topic = topic


def step_events(cam: Onvif, f: Findings, seconds: int) -> None:
    section(f"Live events for {seconds} s: press the doorbell now")
    tev = 'xmlns="http://www.onvif.org/ver10/events/wsdl"'
    sub = cam.call(
        "events",
        f"<CreatePullPointSubscription {tev}><InitialTerminationTime>PT{seconds + 60}S</InitialTerminationTime></CreatePullPointSubscription>",
    )
    address = text(first(sub, "SubscriptionReference"), "Address")
    print(f"subscription {address}")
    try:
        cam.call(
            address,
            f"<SetSynchronizationPoint {tev}/>",
            action="http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/SetSynchronizationPointRequest",
        )
    except OnvifError as err:
        print(f"SetSynchronizationPoint: {err}")
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            try:
                root = cam.call(
                    address,
                    f"<PullMessages {tev}><Timeout>PT5S</Timeout><MessageLimit>32</MessageLimit></PullMessages>",
                    action=ACTION_PULL,
                    timeout=15,
                )
            except OnvifError as err:
                print(f"PullMessages: {err}")
                time.sleep(1)
                continue
            received = dt.datetime.now(dt.UTC)
            for msg in iter_local(root, "NotificationMessage"):
                topic_node = first(msg, "Topic")
                topic = (topic_node.text or "").strip() if topic_node is not None else "?"
                message = first(first(msg, "Message"), "Message")
                utc = message.get("UtcTime", "") if message is not None else ""
                op = message.get("PropertyOperation", "") if message is not None else ""
                items = []
                for block in ("Source", "Data"):
                    node = first(message, block)
                    if node is None:
                        continue
                    for item in iter_local(node, "SimpleItem"):
                        items.append(f"{block[0]}:{item.get('Name')}={item.get('Value')}")
                delay = ""
                if utc:
                    try:
                        sent = dt.datetime.fromisoformat(utc.replace("Z", "+00:00"))
                        delay = f" delay {((received - sent).total_seconds() + cam.offset) * 1000:.0f} ms"
                    except ValueError:
                        pass
                stamp = received.strftime("%H:%M:%S.%f")[:-3]
                line = f"{stamp} {topic} {op} {' '.join(items)}{delay}"
                print(line)
                if "Ring" in topic:
                    f.ring_seen.append(line)
    finally:
        with suppress(OnvifError):
            cam.call(
                address,
                '<Unsubscribe xmlns="http://docs.oasis-open.org/wsn/b-2"/>',
                action=ACTION_UNSUB,
            )


def describe(host: str, port: int, user: str, password: str, uri: str, backchannel: bool):
    rtsp = Rtsp(host, port, user, password)
    try:
        headers = {"Accept": "application/sdp"}
        if backchannel:
            headers["Require"] = REQUIRE_BACKCHANNEL
        return rtsp.request("DESCRIBE", uri, headers)
    finally:
        rtsp.close()


def step_rtsp(args, f: Findings) -> list[str]:
    section("RTSP")
    uris = [u for u in f.stream_uris if u.startswith("rtsp://")]
    if not uris and args.mac:
        mac = args.mac.replace(":", "").lower()
        uris = [
            f"rtsp://{args.host}:{args.rtsp_port}/{mac}-0_m",
            f"rtsp://{args.host}:{args.rtsp_port}/{mac}-0_s",
        ]
    for uri in uris:
        clean = re.sub(r"//[^/@]+@", "//", uri)
        resp = describe(
            args.host, args.rtsp_port, args.user, args.password, clean, backchannel=False
        )
        print(f"DESCRIBE {clean} -> {resp.status} {resp.reason}")
        for media in parse_sdp(resp.body.decode(errors="replace")):
            print(
                f"    m={media.kind} {media.direction} {media.payloads} control={media.control} fmtp={[x[:80] for x in media.fmtp]}"
            )
            if media.kind == "audio" and f.audio_codec == "?":
                f.audio_codec = ", ".join(media.payloads)
        resp_bc = describe(
            args.host, args.rtsp_port, args.user, args.password, clean, backchannel=True
        )
        print(f"DESCRIBE + Require backchannel -> {resp_bc.status} {resp_bc.reason}")
        if resp_bc.status == 200:
            plain = parse_sdp(resp.body.decode(errors="replace"))
            withbc = parse_sdp(resp_bc.body.decode(errors="replace"))
            for media in withbc:
                print(
                    f"    m={media.kind} {media.direction} {media.payloads} control={media.control}"
                )
            extra = [
                m for m in withbc if m.kind == "audio" and m.direction in ("sendonly", "recvonly")
            ]
            if len(withbc) > len(plain) or extra:
                f.backchannel = "YES: " + "; ".join(
                    f"{m.direction} {m.payloads}" for m in extra or withbc[len(plain) :]
                )
            else:
                f.backchannel = "no extra audio track offered"
        else:
            f.backchannel = f"refused ({resp_bc.status} {resp_bc.reason})"
    return [re.sub(r"//[^/@]+@", "//", u) for u in uris]


def nal_types(payload: bytes) -> list[int]:
    if not payload:
        return []
    nal = payload[0] & 0x1F
    if nal == 28 and len(payload) > 1:  # FU-A: only count the start fragment
        return [payload[1] & 0x1F] if payload[1] & 0x80 else []
    if nal == 24:  # STAP-A
        out, i = [], 1
        while i + 2 <= len(payload):
            size = int.from_bytes(payload[i : i + 2], "big")
            if size and i + 2 < len(payload):
                out.append(payload[i + 2] & 0x1F)
            i += 2 + size
        return out
    return [nal]


def step_stream(args, f: Findings, uri: str) -> None:
    section(f"Stream analysis, {args.stream} s of {uri.rsplit('/', 1)[-1]}")
    rtsp = Rtsp(args.host, args.rtsp_port, args.user, args.password)
    try:
        resp = rtsp.request("DESCRIBE", uri, {"Accept": "application/sdp"})
        base = resp.headers.get("content-base", uri)
        medias = [
            m for m in parse_sdp(resp.body.decode(errors="replace")) if m.kind in ("video", "audio")
        ]
        channels: dict[int, Media] = {}
        for index, media in enumerate(medias):
            setup = rtsp.request(
                "SETUP",
                media_url(base, media.control),
                {"Transport": f"RTP/AVP/TCP;unicast;interleaved={index * 2}-{index * 2 + 1}"},
            )
            print(f"SETUP {media.kind} -> {setup.status} {setup.headers.get('transport', '')}")
            channels[index * 2] = media
        started = time.monotonic()
        play = rtsp.request("PLAY", base, {"Range": "npt=0.000-"})
        print(f"PLAY -> {play.status}")
        stats: dict[int, dict] = {
            ch: {"bytes": 0, "packets": 0, "pt": set(), "ts": set()} for ch in channels
        }
        idr_ts: list[int] = []
        first_idr = None
        for channel, payload, received in rtsp.read_interleaved(started + args.stream):
            if channel not in stats or len(payload) < 12:
                continue
            cc = payload[0] & 0x0F
            header = 12 + 4 * cc
            if payload[0] & 0x10 and len(payload) >= header + 4:  # RTP header extension
                header += 4 + 4 * int.from_bytes(payload[header + 2 : header + 4], "big")
            s = stats[channel]
            s["bytes"] += len(payload) - header
            s["packets"] += 1
            s["pt"].add(payload[1] & 0x7F)
            s["ts"].add(int.from_bytes(payload[4:8], "big"))
            if channels[channel].kind == "video" and 5 in nal_types(payload[header:]):
                rtp_ts = int.from_bytes(payload[4:8], "big")
                if not idr_ts or idr_ts[-1] != rtp_ts:
                    idr_ts.append(rtp_ts)
                if first_idr is None:
                    first_idr = received - started
        elapsed = time.monotonic() - started
        for channel, s in stats.items():
            media = channels[channel]
            kbps = s["bytes"] * 8 / 1000 / max(elapsed, 0.001)
            frames = len(s["ts"]) / max(elapsed, 0.001)
            print(
                f"{media.kind}: {s['packets']} packets, {kbps:.0f} kbps, PT {sorted(s['pt'])}, {frames:.1f} timestamps/s, {media.payloads}"
            )
            if media.kind == "video":
                f.bitrate = f"{kbps:.0f} kbps, {frames:.1f} fps"
        if first_idr is not None:
            f.startup = f"first keyframe {first_idr * 1000:.0f} ms after PLAY"
            print(f.startup)
        if len(idr_ts) >= 2:
            gaps = [((b - a) & 0xFFFFFFFF) / 90000 for a, b in itertools.pairwise(idr_ts)]
            f.gop = f"{sum(gaps) / len(gaps):.2f} s between keyframes ({len(idr_ts)} keyframes)"
            print(f"GOP: {f.gop}")
        rtsp.request("TEARDOWN", base)
    finally:
        rtsp.close()


def step_beep(args, f: Findings, uri: str) -> None:
    section("Talk-back test: 1 s beep through the doorbell speaker")
    rtsp = Rtsp(args.host, args.rtsp_port, args.user, args.password)
    try:
        resp = rtsp.request(
            "DESCRIBE", uri, {"Accept": "application/sdp", "Require": REQUIRE_BACKCHANNEL}
        )
        if resp.status != 200:
            f.beep = f"DESCRIBE with backchannel refused ({resp.status})"
            print(f.beep)
            return
        base = resp.headers.get("content-base", uri)
        medias = parse_sdp(resp.body.decode(errors="replace"))
        back = next(
            (m for m in medias if m.kind == "audio" and m.direction == "sendonly"), None
        ) or next((m for m in medias if m.kind == "audio" and m.direction == "recvonly"), None)
        if back is None:
            f.beep = "no backchannel track in SDP"
            print(f.beep)
            return
        codec = None
        for payload in back.payloads:
            pt, name = payload.split(" ", 1)
            upper = name.upper()
            if upper.startswith("PCMA/8000"):
                codec = (int(pt), linear_to_alaw)
                break
            if upper.startswith("PCMU/8000"):
                codec = (int(pt), linear_to_ulaw)
        if codec is None:
            f.beep = f"backchannel codec {back.payloads} not supported by this probe"
            print(f.beep)
            return
        setup = rtsp.request(
            "SETUP",
            media_url(base, back.control),
            {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1", "Require": REQUIRE_BACKCHANNEL},
        )
        print(f"SETUP backchannel -> {setup.status} {setup.headers.get('transport', '')}")
        play = rtsp.request("PLAY", base, {"Range": "npt=0.000-", "Require": REQUIRE_BACKCHANNEL})
        print(f"PLAY -> {play.status}")
        if play.status != 200:
            f.beep = f"PLAY refused ({play.status})"
            return
        pt, encoder = codec
        audio = tone(encoder, 1.0) + tone(encoder, 0.3, freq=0.0)
        ssrc = secrets.randbits(32)
        seq, ts = secrets.randbits(16), secrets.randbits(32)
        start = time.monotonic()
        for i in range(0, len(audio), 160):  # 20 ms packets at 8 kHz
            chunk = audio[i : i + 160]
            rtp = (
                struct.pack(
                    ">BBHII",
                    0x80,
                    pt | (0x80 if i == 0 else 0),
                    seq & 0xFFFF,
                    ts & 0xFFFFFFFF,
                    ssrc,
                )
                + chunk
            )
            rtsp.sock.sendall(b"$" + struct.pack(">BH", 0, len(rtp)) + rtp)
            seq += 1
            ts += len(chunk)
            delay = start + (i // 160 + 1) * 0.02 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        rtsp.request("TEARDOWN", base)
        f.beep = f"sent 1 s tone as PT {pt}: did you hear it at the door?"
        print(f.beep)
    finally:
        rtsp.close()


def step_nvr(cam: Onvif, args, f: Findings) -> None:
    section("NVR recordings (Profile G)")
    if "search" not in cam.xaddr:
        print("no search service: Profile G not available on this device/firmware")
        return
    tse = 'xmlns="http://www.onvif.org/ver10/search/wsdl"'
    summary = cam.call("search", f"<GetRecordingSummary {tse}/>")
    print(
        f"summary: from {text(summary, 'DataFrom')} until {text(summary, 'DataUntil')}, {text(summary, 'NumberRecordings')} recording(s)"
    )
    found = cam.call(
        "search",
        f"<FindRecordings {tse}><Scope/><MaxMatches>32</MaxMatches><KeepAliveTime>PT10S</KeepAliveTime></FindRecordings>",
    )
    token = text(found, "SearchToken")
    results = cam.call(
        "search",
        f"<GetRecordingSearchResults {tse}><SearchToken>{token}</SearchToken><MinResults>1</MinResults><MaxResults>32</MaxResults><WaitTime>PT5S</WaitTime></GetRecordingSearchResults>",
    )
    recordings = []
    for rec in iter_local(results, "RecordingInformation"):
        rtoken = text(rec, "RecordingToken")
        recordings.append(rtoken)
        print(
            f"- {rtoken}: {text(first(rec, 'Source'), 'Name')} {text(rec, 'EarliestRecording')} -> {text(rec, 'LatestRecording')} {text(rec, 'RecordingStatus')}"
        )
    f.profile_g = f"{len(recordings)} recording(s) listed"
    if not recordings or "replay" not in cam.xaddr:
        return
    trp = 'xmlns="http://www.onvif.org/ver10/replay/wsdl"'
    replay = cam.call(
        "replay",
        f"<GetReplayUri {trp}><StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></StreamSetup><RecordingToken>{recordings[0]}</RecordingToken></GetReplayUri>",
    )
    uri = text(replay, "Uri")
    print(f"replay uri: {hide(uri)}")
    clean = re.sub(r"//[^/@]+@", "//", uri)
    for require in (None, "onvif-replay"):
        rtsp = Rtsp(args.host, args.rtsp_port, args.user, args.password)
        try:
            headers = {"Accept": "application/sdp"}
            if require:
                headers["Require"] = require
            resp = rtsp.request("DESCRIBE", clean, headers)
            print(f"DESCRIBE replay (Require={require}) -> {resp.status} {resp.reason}")
            for media in parse_sdp(resp.body.decode(errors="replace")):
                print(f"    m={media.kind} {media.payloads}")
        except OSError as err:
            print(f"DESCRIBE replay: {err}")
        finally:
            rtsp.close()


# ------------------------------------------------------------------------- main


def run(args) -> Findings:
    f = Findings()
    cam = Onvif(args.host, args.onvif_port, args.user, args.password)
    steps = [
        ("device", lambda: step_device(cam, f)),
        ("media", lambda: step_media(cam, f)),
        ("topics", lambda: step_topics(cam, f)),
    ]
    for name, step in steps:
        try:
            step()
        except (OnvifError, OSError, ET.ParseError) as err:
            f.errors.append(f"{name}: {err}")
            print(f"!! {name}: {err}")
    uris: list[str] = []
    try:
        uris = step_rtsp(args, f)
    except OSError as err:
        f.errors.append(f"rtsp: {err}")
        print(f"!! rtsp: {err}")
    if uris and args.stream:
        try:
            step_stream(args, f, uris[0])
        except OSError as err:
            f.errors.append(f"stream: {err}")
            print(f"!! stream: {err}")
    if args.nvr:
        try:
            step_nvr(cam, args, f)
        except (OnvifError, OSError, ET.ParseError) as err:
            f.errors.append(f"nvr: {err}")
            print(f"!! nvr: {err}")
    if args.events:
        try:
            step_events(cam, f, args.events)
        except (OnvifError, OSError, ET.ParseError) as err:
            f.errors.append(f"events: {err}")
            print(f"!! events: {err}")
    if args.beep and uris:
        try:
            step_beep(args, f, uris[0])
        except OSError as err:
            f.errors.append(f"beep: {err}")
            print(f"!! beep: {err}")
    return f


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default="192.168.128.31")
    parser.add_argument("--onvif-port", type=int, default=8080)
    parser.add_argument("--rtsp-port", type=int, default=8554)
    parser.add_argument(
        "--mac", default="9c:75:6e:12:34:56", help="fallback RTSP path when GetStreamUri fails"
    )
    parser.add_argument(
        "--events", type=int, default=90, help="seconds to listen to events (0 = skip)"
    )
    parser.add_argument(
        "--stream", type=int, default=6, help="seconds of stream analysis (0 = skip)"
    )
    parser.add_argument(
        "--beep", action="store_true", help="play a 1 s tone through the doorbell speaker"
    )
    parser.add_argument("--nvr", action="store_true", help="query Profile G recording services")
    parser.add_argument("--report", default="", help="also save the report to this file")
    args = parser.parse_args()
    args.user = os.environ.get("AJAX_ONVIF_USER", "")
    args.password = os.environ.get("AJAX_ONVIF_PASS", "")
    if not args.user or not args.password:
        print(
            "Set AJAX_ONVIF_USER and AJAX_ONVIF_PASS (run_phase0.sh asks for them).",
            file=sys.stderr,
        )
        return 2

    out = io.StringIO()

    class Tee(io.TextIOBase):
        def write(self, s):
            sys.__stdout__.write(s)
            sys.__stdout__.flush()
            out.write(s)
            return len(s)

    with redirect_stdout(Tee()):
        print(
            f"Ajax ONVIF probe {dt.datetime.now().isoformat(timespec='seconds')} host {args.host} user {args.user}"
        )
        f = run(args)
        section("Summary")
        print(f"model           {f.model} firmware {f.firmware}")
        print(f"stream URIs     {[hide(u) for u in f.stream_uris] or 'none from ONVIF'}")
        print(f"snapshot        {hide(f.snapshot) or 'none'}")
        print(f"audio codec     {f.audio_codec}")
        print(f"backchannel     {f.backchannel}")
        print(f"ring topic      {f.ring_topic}")
        print(
            f"ring events     {len(f.ring_seen)} seen"
            + (f": {f.ring_seen[0]}" if f.ring_seen else "")
        )
        print(f"video           {f.bitrate}; {f.gop}; {f.startup}")
        print(f"profile G       {f.profile_g}")
        print(f"beep            {f.beep}")
        if f.errors:
            print(f"errors          {f.errors}")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(out.getvalue())
        print(f"\nReport saved to {args.report} (no password inside).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
