"""Replay the DoorBell's recordings from the Ajax NVR, storing nothing locally.

The NVR keeps the video (ONVIF Profile G). To play a moment, go2rtc (the card,
over WebRTC) or ffmpeg (a thumbnail) opens an RTSP URL on a small relay bound
to 127.0.0.1. The relay replays the NVR's recording from that moment and adds
what a plain RTSP client cannot send: the onvif-replay requirement, a
wall-clock Range, the ONVIF account, and track directions go2rtc accepts (it
ignores "sendrecv" tracks, which is how the NVR marks them).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
from datetime import UTC, datetime
import logging
import re
from urllib.parse import urlsplit

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .onvif import AjaxOnvifClient, Recording
from .rtsp import RtspError, _digest_header, _parse_head

_LOGGER = logging.getLogger(__name__)

STAMP = "%Y%m%dT%H%M%SZ"
_PATH = re.compile(r"^/replay/(main|sub)/(\d{8}T\d{6}Z)(/.*)?$")
_THUMBS = 120  # in memory only, about 30 kB each
_TIMEOUT = 10


class NvrError(Exception):
    """The NVR cannot replay this DoorBell."""


def _group(token: str) -> str:
    """Return the camera part of a recording token: "<camera>-main-r" -> "<camera>"."""
    return re.sub(r"-(main|sub)-r$", "", token)


def pick_recordings(recordings: list[Recording], mac: str | None) -> dict[str, Recording]:
    """Return the DoorBell's recordings by quality ("main", "sub").

    Ajax names them after the camera's MAC; without a match, a NVR that
    records a single camera is assumed to record this one.
    """
    compact = (mac or "").replace(":", "").lower()
    mine = [r for r in recordings if compact and compact in r.token.lower()]
    if not mine and len({_group(r.token) for r in recordings}) == 1:
        mine = recordings
    if not mine:
        raise NvrError("no recording of this DoorBell on the NVR")
    picked: dict[str, Recording] = {}
    for recording in mine:
        quality = "sub" if recording.token.endswith("-sub-r") else "main"
        picked.setdefault(quality, recording)
    picked.setdefault("main", next(iter(picked.values())))
    return picked


class AjaxNvr:
    """The NVR that records the DoorBell, and the local replay relay."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        username: str,
        password: str,
        mac: str | None,
    ) -> None:
        """Prepare the client; nothing is contacted yet."""
        self.host = host
        self.client = AjaxOnvifClient(async_get_clientsession(hass), host, port, username, password)
        self._username, self._password = username, password
        self._mac = mac
        self.recordings: dict[str, Recording] = {}
        self.replay_uris: dict[str, str] = {}
        self.port = 0
        self._server: asyncio.Server | None = None
        self._relays: set[asyncio.Task[None]] = set()
        self._thumbs: OrderedDict[str, bytes] = OrderedDict()
        self._thumb_lock = asyncio.Lock()

    async def async_initialize(self) -> None:
        """Find the DoorBell's recordings, then open the relay."""
        client = self.client
        await client.async_update_time_offset()
        services = await client.async_get_services()
        if not {"search", "replay"} <= services.keys():
            raise NvrError(f"{self.host} has no recording services (ONVIF Profile G)")
        self.recordings = pick_recordings(await client.async_find_recordings(), self._mac)
        for quality, recording in self.recordings.items():
            self.replay_uris[quality] = await client.async_get_replay_uri(recording.token)
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        _LOGGER.debug(
            "NVR %s: replaying %s through 127.0.0.1:%s",
            self.host,
            {q: r.token for q, r in self.recordings.items()},
            self.port,
        )

    async def async_close(self) -> None:
        """Close the relay and every replay going through it."""
        if self._server is not None:
            self._server.close()
            self._server = None
        for task in list(self._relays):
            task.cancel()
        if self._relays:
            await asyncio.wait(self._relays, timeout=2)

    def replay_url(self, start: datetime, quality: str = "main") -> str:
        """Return the local RTSP URL that plays the recording from this moment."""
        if quality not in self.replay_uris:
            quality = "main"
        return f"rtsp://127.0.0.1:{self.port}/replay/{quality}/{start.astimezone(UTC):{STAMP}}"

    async def async_thumbnail(self, start: datetime) -> bytes | None:
        """Return a small JPEG of the recording at this moment (kept in memory)."""
        key = f"{start.astimezone(UTC):{STAMP}}"
        if (image := self._thumbs.get(key)) is not None:
            self._thumbs.move_to_end(key)
            return image
        async with self._thumb_lock:  # one ffmpeg at a time
            if (image := self._thumbs.get(key)) is not None:
                return image
            image = await self._grab(self.replay_url(start, "sub"))
        if image:
            self._thumbs[key] = image
            while len(self._thumbs) > _THUMBS:
                self._thumbs.popitem(last=False)
        return image

    async def _grab(self, url: str) -> bytes | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                url,
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                "-q:v",
                "5",
                "-f",
                "image2",
                "-c:v",
                "mjpeg",
                "pipe:1",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as err:
            _LOGGER.debug("ffmpeg unavailable for thumbnails: %s", err)
            return None
        try:
            async with asyncio.timeout(15):
                image, errors = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            _LOGGER.debug("Thumbnail from %s timed out", url)
            return None
        if process.returncode or not image:
            _LOGGER.debug(
                "Thumbnail from %s failed: %s", url, errors.decode(errors="replace")[-200:]
            )
            return None
        return image

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._relays.add(task)
        relay = _ReplayRelay(self, reader, writer)
        try:
            await relay.run()
        except (OSError, asyncio.IncompleteReadError, ValueError, RtspError, TimeoutError) as err:
            _LOGGER.debug("Replay relay closed: %s", err or type(err).__name__)
        finally:
            await relay.close()
            self._relays.discard(task)


_HEADER_NAMES = {
    "cseq": "CSeq",
    "www-authenticate": "WWW-Authenticate",
    "rtp-info": "RTP-Info",
}


def _header_name(key: str) -> str:
    return _HEADER_NAMES.get(key, "-".join(part.capitalize() for part in key.split("-")))


def rewrite_sdp(body: bytes, upstream: str, local: str) -> bytes:
    """Keep video and audio, received only, with URLs pointing at the relay."""
    sections = re.split(r"(?m)^(?=m=)", body.decode(errors="replace"))
    kept = [sections[0]] + [s for s in sections[1:] if s.startswith(("m=video", "m=audio"))]
    text = "".join(kept).replace("a=sendrecv", "a=recvonly").replace("a=sendonly", "a=recvonly")
    return text.replace(upstream, local).encode()


class _ReplayRelay:
    """One RTSP client (go2rtc, ffmpeg) relayed to the NVR's replay of one moment."""

    def __init__(
        self, nvr: AjaxNvr, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._nvr = nvr
        self._reader, self._writer = reader, writer
        self._up_reader: asyncio.StreamReader | None = None
        self._up_writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[tuple[str, dict[str, str], bytes]]] = {}
        self._cseq = 0
        self._challenge: str | None = None
        # The NVR starts the media right after its PLAY reply: hold it until the
        # client got that reply, or the client would see media first.
        self._released = asyncio.Event()

    async def run(self) -> None:
        """Answer the client's requests until it leaves."""
        while True:
            first = await self._reader.readexactly(1)
            if first == b"$":  # RTCP from the client: pass it on
                header = await self._reader.readexactly(3)
                data = await self._reader.readexactly(int.from_bytes(header[1:], "big"))
                if self._up_writer is not None:
                    self._up_writer.write(first + header + data)
                continue
            line, headers = _parse_head(first + await self._reader.readuntil(b"\r\n\r\n"))
            length = int(headers.get("content-length", "0") or 0)
            body = await self._reader.readexactly(length) if length else b""
            await self._answer(line, headers, body)

    async def _answer(self, line: str, headers: dict[str, str], body: bytes) -> None:
        method, url = ([*line.split(" "), "", ""])[:2]
        cseq = headers.get("cseq", "0")
        parts = urlsplit(url)
        match = _PATH.match(parts.path)
        if match is None or match.group(1) not in self._nvr.replay_uris:
            self._reply(cseq, "404 Not Found")
            return
        quality, stamp, suffix = match.groups()
        local = f"{parts.scheme}://{parts.netloc}/replay/{quality}/{stamp}"
        upstream = self._nvr.replay_uris[quality].rstrip("/")
        if method == "SETUP" and "tcp" not in headers.get("transport", "").lower():
            self._reply(cseq, "461 Unsupported Transport")  # ffmpeg then retries over TCP
            return
        out = {
            key: value
            for key, value in headers.items()
            if key not in ("cseq", "require", "authorization", "content-length", "range")
        }
        out["require"] = "onvif-replay"
        if method == "PLAY":
            out["range"] = f"clock={stamp}-"
        status, reply, reply_body = await self._forward(
            method, upstream + (suffix or ""), out, body
        )
        reply = {key: value.replace(upstream, local) for key, value in reply.items()}
        if method == "DESCRIBE" and status.startswith("200"):
            reply_body = rewrite_sdp(reply_body, upstream, local)
        self._reply(cseq, status, reply, reply_body)
        self._released.set()

    def _reply(
        self, cseq: str, status: str, headers: dict[str, str] | None = None, body: bytes = b""
    ) -> None:
        lines = [f"RTSP/1.0 {status}", f"CSeq: {cseq}"]
        lines += [
            f"{_header_name(key)}: {value}"
            for key, value in (headers or {}).items()
            if key not in ("cseq", "content-length")
        ]
        if body:
            lines.append(f"Content-Length: {len(body)}")
        self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)

    async def _forward(
        self, method: str, url: str, headers: dict[str, str], body: bytes
    ) -> tuple[str, dict[str, str], bytes]:
        """Send a request to the NVR, answering its Digest challenge once."""
        if self._up_writer is None:
            parts = urlsplit(url)
            async with asyncio.timeout(_TIMEOUT):
                self._up_reader, self._up_writer = await asyncio.open_connection(
                    parts.hostname, parts.port or 554
                )
            self._pump = asyncio.get_running_loop().create_task(self._run_pump())
        for attempt in range(2):
            self._cseq += 1
            lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self._cseq}"]
            if self._challenge:
                digest = _digest_header(
                    self._challenge,
                    method,
                    url,
                    self._nvr._username,
                    self._nvr._password,
                    self._cseq,
                )
                lines.append(f"Authorization: {digest}")
            lines += [f"{_header_name(key)}: {value}" for key, value in headers.items()]
            if body:
                lines.append(f"Content-Length: {len(body)}")
            future = asyncio.get_running_loop().create_future()
            self._pending[self._cseq] = future
            self._up_writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
            async with asyncio.timeout(_TIMEOUT):
                status, reply, reply_body = await future
            challenge = reply.get("www-authenticate", "")
            if (
                status.startswith("401")
                and attempt == 0
                and challenge.startswith("Digest")
                and (challenge != self._challenge or "stale=true" in challenge.lower())
            ):
                self._challenge = challenge
                self._released.set()
                continue
            return status, reply, reply_body
        return status, reply, reply_body

    async def _run_pump(self) -> None:
        """Pass the NVR's media to the client and its replies to _forward."""
        reader = self._up_reader
        assert reader is not None
        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":
                    header = await reader.readexactly(3)
                    data = await reader.readexactly(int.from_bytes(header[1:], "big"))
                    self._writer.write(first + header + data)
                    await self._writer.drain()
                    continue
                line, headers = _parse_head(first + await reader.readuntil(b"\r\n\r\n"))
                length = int(headers.get("content-length", "0") or 0)
                body = await reader.readexactly(length) if length else b""
                if not line.startswith("RTSP/"):
                    continue  # a request from the NVR: nothing to do with it
                status = line.split(" ", 1)[1] if " " in line else ""
                cseq = int(headers.get("cseq", "-1") or -1)
                if (future := self._pending.pop(cseq, None)) is not None and not future.done():
                    self._released.clear()
                    future.set_result((status, headers, body))
                    await self._released.wait()
        except (OSError, asyncio.IncompleteReadError, ValueError) as err:
            _LOGGER.debug("NVR replay stream ended: %s", err or type(err).__name__)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RtspError("NVR connection closed"))
            self._pending.clear()
            self._writer.close()  # the client sees the end of the recording

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            with suppress(asyncio.CancelledError):
                await self._pump
        for writer in (self._up_writer, self._writer):
            if writer is not None:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
