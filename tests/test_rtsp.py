"""Tests for the RTSP DESCRIBE probe, against a small local RTSP server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import hashlib
import re

import pytest

from custom_components.ajax_doorbell.rtsp import (
    REQUIRE_BACKCHANNEL,
    RtspError,
    async_describe,
    parse_sdp,
    strip_credentials,
)

SDP = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Ajax\r\nt=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=0\r\na=recvonly\r\n"
    "m=audio 0 RTP/AVP 9\r\na=rtpmap:9 G722/8000\r\na=control:trackID=1\r\na=recvonly\r\n"
    "m=audio 0 RTP/AVP 8 0\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\n"
    "a=control:trackID=2\r\na=sendonly\r\n"
)


def test_parse_sdp() -> None:
    """Directions and codecs are read per track."""
    video, audio, back = parse_sdp(SDP)
    assert (video.kind, video.direction, video.codecs) == ("video", "recvonly", ("H264/90000",))
    assert audio.codec_names == {"G722"}
    assert (back.direction, back.codecs, back.control) == (
        "sendonly",
        ("PCMA/8000", "PCMU/8000"),
        "trackID=2",
    )


def test_strip_credentials() -> None:
    """Credentials never leave in logs or diagnostics."""
    assert strip_credentials("rtsp://u:p%40ss@h:8554/x_m") == "rtsp://h:8554/x_m"
    assert strip_credentials("rtsp://h:8554/x_m") == "rtsp://h:8554/x_m"


Handler = Callable[[str], str]


@pytest.fixture
async def rtsp_server(socket_enabled: None) -> AsyncIterator[Callable[[Handler], asyncio.Future]]:
    """Start a one-handler RTSP server; yield a function that sets the handler."""
    servers: list[asyncio.Server] = []

    async def start(handler: Handler) -> int:
        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while True:
                    request = (await reader.readuntil(b"\r\n\r\n")).decode()
                    writer.write(handler(request).encode())
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        servers.append(server)
        return server.sockets[0].getsockname()[1]

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()


def _reply(cseq: str, status: str, headers: str = "", body: str = "") -> str:
    extra = (
        f"Content-Type: application/sdp\r\nContent-Length: {len(body.encode())}\r\n" if body else ""
    )
    return f"RTSP/1.0 {status}\r\nCSeq: {cseq}\r\n{headers}{extra}\r\n{body}"


async def test_describe_with_digest_and_backchannel(rtsp_server) -> None:
    """The client answers the Digest challenge and asks for the backchannel."""
    seen: list[str] = []

    def handler(request: str) -> str:
        seen.append(request)
        cseq = re.search(r"CSeq: (\d+)", request).group(1)
        auth = re.search(r'response="([0-9a-f]+)"', request)
        if not auth:
            return _reply(
                cseq,
                "401 Unauthorized",
                'WWW-Authenticate: Digest realm="Ajax RTSP Server", nonce="abc"\r\n',
            )
        uri = request.split(" ")[1]
        ha1 = hashlib.md5(b"homeassistant:Ajax RTSP Server:secret").hexdigest()
        ha2 = hashlib.md5(f"DESCRIBE:{uri}".encode()).hexdigest()
        expected = hashlib.md5(f"{ha1}:abc:{ha2}".encode()).hexdigest()
        assert auth.group(1) == expected
        return _reply(cseq, "200 OK", body=SDP)

    port = await rtsp_server(handler)
    description = await async_describe(
        f"rtsp://homeassistant:secret@127.0.0.1:{port}/9c756e123456-0_m",
        "homeassistant",
        "secret",
        backchannel=True,
    )
    assert description.status == 200
    assert description.backchannel is not None
    assert description.backchannel.codec_names == {"PCMA", "PCMU"}
    assert description.audio.codec_names == {"G722"}
    assert all(f"Require: {REQUIRE_BACKCHANNEL}" in request for request in seen)
    assert all("secret" not in request.split("\r\n")[0] for request in seen)


async def test_describe_backchannel_refused(rtsp_server) -> None:
    """A device without backchannel answers 551: no talk-back, no error."""
    port = await rtsp_server(
        lambda request: _reply(
            re.search(r"CSeq: (\d+)", request).group(1), "551 Option not supported"
        )
    )
    description = await async_describe(f"rtsp://127.0.0.1:{port}/x_m", "u", "p", backchannel=True)
    assert description.status == 551
    assert description.backchannel is None


async def test_describe_unreachable(socket_enabled: None) -> None:
    """A closed port is an RtspError."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with pytest.raises(RtspError):
        await async_describe(f"rtsp://127.0.0.1:{port}/x_m", "u", "p", timeout=2)
