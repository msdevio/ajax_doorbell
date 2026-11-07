"""Minimal async RTSP client: DESCRIBE, and a session on the ONVIF backchannel.

Streaming the video is go2rtc's job. The integration only asks what a stream
carries, and plays the browser's voice through the DoorBell's speaker on the
audio backchannel, a track go2rtc cannot feed in G.722.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import logging
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

_LOGGER = logging.getLogger(__name__)

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
    payload_types: tuple[int, ...] = ()  # one per codec

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
                {
                    "kind": line[2:].split(" ", 1)[0],
                    "dir": "sendrecv",
                    "codecs": [],
                    "types": [],
                    "control": "",
                }
            )
        elif not sections:
            continue
        elif line.startswith("a=rtpmap:"):
            payload_type, _, codec = line[9:].partition(" ")
            if codec not in sections[-1]["codecs"] and payload_type.isdigit():
                sections[-1]["codecs"].append(codec)
                sections[-1]["types"].append(int(payload_type))
        elif line.startswith("a=control:"):
            sections[-1]["control"] = line[10:]
        elif line[2:] in _DIRECTIONS:
            sections[-1]["dir"] = line[2:]
    return tuple(
        SdpMedia(s["kind"], s["dir"], tuple(s["codecs"]), s["control"], tuple(s["types"]))
        for s in sections
    )


def media_url(base: str, control: str) -> str:
    """Return the URL of a track: its control attribute against the content base."""
    if not control or control == "*":
        return base
    if control.startswith("rtsp://"):
        return control
    return base.rstrip("/") + "/" + control


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


def _parse_head(head: bytes) -> tuple[str, dict[str, str]]:
    lines = head.decode(errors="replace").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return lines[0], headers


class RtspConnection:
    """One RTSP control connection (Digest auth, a session, interleaved RTP out)."""

    def __init__(self, uri: str, username: str, password: str, *, timeout: float = 8) -> None:
        """Prepare the connection; nothing is opened yet."""
        self.uri = strip_credentials(uri)
        parts = urlsplit(self.uri)
        if parts.scheme != "rtsp" or not parts.hostname:
            raise RtspError(f"not an RTSP URI: {self.uri}")
        self.host, self.port = parts.hostname, parts.port or 554
        self._username, self._password = username, password
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._cseq = 0
        self._challenge: str | None = None
        self.session: str | None = None
        self.session_timeout = 60
        self._pending: dict[int, asyncio.Future[tuple[int, dict[str, str], bytes]]] = {}
        self._pump: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()

    async def connect(self) -> None:
        """Open the TCP connection."""
        try:
            async with asyncio.timeout(self._timeout):
                self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        except (OSError, TimeoutError) as err:
            raise RtspError(f"{self.host}: {str(err) or type(err).__name__}") from err

    async def _read_message(self) -> tuple[int, dict[str, str], bytes] | None:
        """Read one reply, or skip one interleaved frame (None)."""
        assert self._reader is not None
        first = await self._reader.readexactly(1)
        if first == b"$":
            header = await self._reader.readexactly(3)
            await self._reader.readexactly(int.from_bytes(header[1:], "big"))
            return None
        status_line, headers = _parse_head(first + await self._reader.readuntil(b"\r\n\r\n"))
        length = int(headers.get("content-length", "0") or 0)
        body = await self._reader.readexactly(length) if length else b""
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("RTSP/") or not parts[1].isdigit():
            return (0, headers, body)  # a request from the server: ignored
        return (int(parts[1]), headers, body)

    async def _reply(self, cseq: int) -> tuple[int, dict[str, str], bytes]:
        if self._pump is not None:
            future = asyncio.get_running_loop().create_future()
            self._pending[cseq] = future
            return await future
        while True:
            message = await self._read_message()
            if message is not None and message[0]:
                return message

    async def request(
        self, method: str, uri: str | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        """Send a request (answering a Digest challenge once) and return the reply."""
        if self._writer is None:
            raise RtspError(f"{self.host}: not connected")
        target = uri or self.uri
        try:
            async with asyncio.timeout(self._timeout):
                for attempt in range(2):
                    self._cseq += 1
                    lines = [
                        f"{method} {target} RTSP/1.0",
                        f"CSeq: {self._cseq}",
                        "User-Agent: Home Assistant ajax_doorbell",
                    ]
                    if self.session:
                        lines.append(f"Session: {self.session}")
                    if self._challenge:
                        authorization = _digest_header(
                            self._challenge,
                            method,
                            target,
                            self._username,
                            self._password,
                            self._cseq,
                        )
                        lines.append(f"Authorization: {authorization}")
                    lines += [f"{key}: {value}" for key, value in (headers or {}).items()]
                    self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
                    await self._writer.drain()
                    status, reply, body = await self._reply(self._cseq)
                    challenge = reply.get("www-authenticate", "")
                    if (
                        status == 401
                        and attempt == 0
                        and challenge.startswith("Digest")
                        and (challenge != self._challenge or "stale=true" in challenge.lower())
                    ):
                        self._challenge = challenge
                        continue
                    break
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError) as err:
            raise RtspError(f"{self.host}: {str(err) or type(err).__name__}") from err
        if session := reply.get("session"):
            self.session, _, rest = session.partition(";")
            if found := re.search(r"timeout=(\d+)", rest):
                self.session_timeout = int(found.group(1))
        return status, reply, body

    def start_pump(self) -> None:
        """After PLAY: read replies in the background and drop incoming frames."""
        self._pump = asyncio.get_running_loop().create_task(self._run_pump())

    async def _run_pump(self) -> None:
        try:
            while True:
                message = await self._read_message()
                if message is None or not message[0]:
                    continue
                cseq = int(message[1].get("cseq", "-1") or -1)
                if (future := self._pending.pop(cseq, None)) and not future.done():
                    future.set_result(message)
        except (OSError, asyncio.IncompleteReadError, ValueError) as err:
            _LOGGER.debug("%s: RTSP session closed: %s", self.host, err)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RtspError(f"{self.host}: connection closed"))
            self._pending.clear()
            self.closed.set()

    @property
    def backlog(self) -> int:
        """Return the bytes written but not yet sent."""
        if self._writer is None or self._writer.transport.is_closing():
            return 0
        return self._writer.transport.get_write_buffer_size()

    def send_interleaved(self, channel: int, packet: bytes) -> None:
        """Queue one RTP packet on an interleaved channel."""
        if self._writer is not None and not self._writer.transport.is_closing():
            self._writer.write(b"$" + bytes((channel,)) + len(packet).to_bytes(2, "big") + packet)

    async def close(self, teardown_uri: str | None = None) -> None:
        """End the session politely, then close the connection."""
        if teardown_uri and self.session and not self.closed.is_set():
            with suppress(RtspError):
                async with asyncio.timeout(2):
                    await self.request("TEARDOWN", teardown_uri)
        if self._pump is not None:
            self._pump.cancel()
            with suppress(asyncio.CancelledError):
                await self._pump
        if self._writer is not None:
            self._writer.close()
            with suppress(OSError):
                await self._writer.wait_closed()
        self.closed.set()


async def async_describe(
    uri: str,
    username: str,
    password: str,
    *,
    backchannel: bool = False,
    timeout: float = 8,
) -> StreamDescription:
    """Send DESCRIBE (with Digest auth) and return the parsed answer."""
    connection = RtspConnection(uri, username, password, timeout=timeout)
    await connection.connect()
    try:
        headers = {"Accept": "application/sdp"}
        if backchannel:
            headers["Require"] = REQUIRE_BACKCHANNEL
        status, _, body = await connection.request("DESCRIBE", headers=headers)
    finally:
        await connection.close()
    medias = parse_sdp(body.decode(errors="replace")) if status == 200 else ()
    return StreamDescription(status, medias)
