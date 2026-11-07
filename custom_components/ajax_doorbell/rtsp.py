"""Minimal async RTSP client: DESCRIBE, with or without the ONVIF backchannel.

The integration only needs to know what a stream carries and whether the
device offers an audio backchannel (the track go2rtc feeds with the browser's
microphone). Streaming itself is go2rtc's job.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

REQUIRE_BACKCHANNEL = "www.onvif.org/ver20/backchannel"
_DIRECTIONS = ("sendrecv", "sendonly", "recvonly", "inactive")


class RtspError(Exception):
    """The RTSP exchange failed."""


@dataclass(frozen=True, slots=True)
class SdpMedia:
    """One m= section of an SDP."""

    kind: str
    direction: str
    codecs: tuple[str, ...]  # "PCMA/8000", "H264/90000"
    control: str

    @property
    def codec_names(self) -> frozenset[str]:
        """Return the upper-case codec names without clock rates."""
        return frozenset(c.split("/", 1)[0].upper() for c in self.codecs)


@dataclass(frozen=True, slots=True)
class StreamDescription:
    """The answer to a DESCRIBE."""

    status: int
    medias: tuple[SdpMedia, ...]

    @property
    def video(self) -> SdpMedia | None:
        """Return the first video track."""
        return next((m for m in self.medias if m.kind == "video"), None)

    @property
    def audio(self) -> SdpMedia | None:
        """Return the first audio track the device sends."""
        return next(
            (
                m
                for m in self.medias
                if m.kind == "audio" and m.direction in ("sendrecv", "recvonly")
            ),
            None,
        )

    @property
    def backchannel(self) -> SdpMedia | None:
        """Return the audio track the device receives (ONVIF marks it sendonly)."""
        return next(
            (m for m in self.medias if m.kind == "audio" and m.direction == "sendonly"),
            None,
        )


def parse_sdp(sdp: str) -> tuple[SdpMedia, ...]:
    """Parse the media sections of an SDP body."""
    sections: list[dict] = []
    for raw in sdp.splitlines():
        line = raw.strip()
        if line.startswith("m="):
            sections.append(
                {"kind": line[2:].split(" ", 1)[0], "dir": "sendrecv", "codecs": [], "control": ""}
            )
        elif not sections:
            continue
        elif line.startswith("a=rtpmap:"):
            codec = line[9:].split(" ", 1)[-1]
            if codec not in sections[-1]["codecs"]:
                sections[-1]["codecs"].append(codec)
        elif line.startswith("a=control:"):
            sections[-1]["control"] = line[10:]
        elif line[2:] in _DIRECTIONS:
            sections[-1]["dir"] = line[2:]
    return tuple(SdpMedia(s["kind"], s["dir"], tuple(s["codecs"]), s["control"]) for s in sections)


def strip_credentials(uri: str) -> str:
    """Remove user:password@ from a URI."""
    parts = urlsplit(uri)
    if "@" not in parts.netloc:
        return uri
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def _digest_header(
    challenge: str, method: str, uri: str, username: str, password: str, count: int
) -> str:
    fields = dict(re.findall(r'(\w+)="?([^",]*)"?', challenge))
    realm, nonce = fields.get("realm", ""), fields.get("nonce", "")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    header = f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}"'
    if "auth" in fields.get("qop", "").split(","):
        cnonce, nc = secrets.token_hex(8), f"{count:08x}"
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        header += f', qop=auth, nc={nc}, cnonce="{cnonce}"'
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    if opaque := fields.get("opaque"):
        header += f', opaque="{opaque}"'
    return header + f', response="{response}"'


async def _read_response(
    reader: asyncio.StreamReader,
) -> tuple[int, dict[str, str], bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("RTSP/"):
        raise RtspError(f"unexpected reply: {lines[0][:60]}")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0") or 0)
    body = await reader.readexactly(length) if length else b""
    return int(parts[1]), headers, body


async def async_describe(
    uri: str,
    username: str,
    password: str,
    *,
    backchannel: bool = False,
    timeout: float = 8,
) -> StreamDescription:
    """Send DESCRIBE (with Digest auth) and return the parsed answer."""
    clean = strip_credentials(uri)
    parts = urlsplit(clean)
    if parts.scheme != "rtsp" or not parts.hostname:
        raise RtspError(f"not an RTSP URI: {clean}")
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(parts.hostname, parts.port or 554)
            try:
                authorization = None
                for count in range(1, 3):
                    lines = [
                        f"DESCRIBE {clean} RTSP/1.0",
                        f"CSeq: {count}",
                        "User-Agent: Home Assistant ajax_doorbell",
                        "Accept: application/sdp",
                    ]
                    if backchannel:
                        lines.append(f"Require: {REQUIRE_BACKCHANNEL}")
                    if authorization:
                        lines.append(f"Authorization: {authorization}")
                    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
                    await writer.drain()
                    status, headers, body = await _read_response(reader)
                    challenge = headers.get("www-authenticate", "")
                    if status == 401 and authorization is None and challenge.startswith("Digest"):
                        authorization = _digest_header(
                            challenge, "DESCRIBE", clean, username, password, count
                        )
                        continue
                    break
            finally:
                writer.close()
    except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError) as err:
        raise RtspError(f"{parts.hostname}: {str(err) or type(err).__name__}") from err
    medias = parse_sdp(body.decode(errors="replace")) if status == 200 else ()
    return StreamDescription(status, medias)
