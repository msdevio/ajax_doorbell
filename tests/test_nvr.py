"""Tests for the NVR replay relay."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ajax_doorbell.nvr import AjaxNvr, NvrError, pick_recordings, rewrite_sdp
from custom_components.ajax_doorbell.onvif import Recording
from homeassistant.core import HomeAssistant

MAC = "9c:75:6e:12:34:56"
TOKEN = "bcFhIz1NqO-9c756e123456-0"
# What the Ajax NVR answers a replay DESCRIBE (firmware 2.803).
SDP = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Replay\r\nt=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:streamid=0\r\na=sendrecv\r\n"
    "m=audio 0 RTP/AVP 9\r\na=rtpmap:9 G722/16000\r\na=control:streamid=1\r\na=sendrecv\r\n"
    "m=application 0 RTP/AVP 107\r\na=rtpmap:107 vnd.onvif.metadata/90000\r\n"
    "a=control:streamid=2\r\na=sendrecv\r\n"
)


def _recording(token: str, source: str = "Sonnette") -> Recording:
    return Recording(token, source, None, None)


def test_pick_recordings() -> None:
    """The DoorBell's recordings are found by MAC, by quality."""
    recordings = [
        _recording("cam-aabbccddeeff-0-main-r", "Garage"),
        _recording(f"{TOKEN}-main-r"),
        _recording(f"{TOKEN}-sub-r"),
    ]
    picked = pick_recordings(recordings, MAC)
    assert (picked["main"].token, picked["sub"].token) == (f"{TOKEN}-main-r", f"{TOKEN}-sub-r")
    # A single camera on the NVR is assumed to be this one.
    assert pick_recordings(recordings[1:], None)["main"].token == f"{TOKEN}-main-r"
    with pytest.raises(NvrError):
        pick_recordings(recordings, "00:11:22:33:44:55")


def test_rewrite_sdp() -> None:
    """Video and audio only, received only, with the relay's URLs."""
    upstream = "rtsp://192.168.1.30:8554/x_rm"
    local = "rtsp://127.0.0.1:1234/replay/main/20261107T112200Z"
    sdp = SDP.replace("a=control:streamid=0", f"a=control:{upstream}/streamid=0").encode()
    text = rewrite_sdp(sdp, upstream, local).decode()
    assert "m=application" not in text
    assert "sendrecv" not in text
    assert text.count("a=recvonly") == 2
    assert f"a=control:{local}/streamid=0" in text


class FakeNvr:
    """Replays a recording; wants Digest auth and Require: onvif-replay."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request = (await reader.readuntil(b"\r\n\r\n")).decode()
                self.requests.append(request)
                method, uri = request.split(" ", 2)[:2]
                cseq = re.search(r"CSeq: (\d+)", request).group(1)
                if "Authorization: Digest" not in request:
                    challenge = 'WWW-Authenticate: Digest realm="Ajax RTSP Server", nonce="n"\r\n'
                    writer.write(
                        f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n{challenge}\r\n".encode()
                    )
                    continue
                if "Require: onvif-replay" not in request:
                    writer.write(
                        f"RTSP/1.0 551 Option not supported\r\nCSeq: {cseq}\r\n\r\n".encode()
                    )
                    continue
                headers, body = "", ""
                if method == "DESCRIBE":
                    body = SDP
                    headers = f"Content-Base: {uri}/\r\nContent-Length: {len(body)}\r\n"
                elif method == "SETUP":
                    transport = re.search(r"Transport: ([^\r]*)", request).group(1)
                    headers = f"Session: 77;timeout=30\r\nTransport: {transport}\r\n"
                elif method == "PLAY":
                    headers = f"RTP-Info: url={uri}/streamid=0;seq=1\r\n"
                writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{headers}\r\n{body}".encode())
                if method == "PLAY":
                    for n in range(3):
                        rtp = bytes([0x80, 96, 0, n]) + bytes(8) + b"frame"
                        writer.write(b"$\x00" + len(rtp).to_bytes(2, "big") + rtp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def nvr(hass: HomeAssistant, socket_enabled: None) -> AsyncIterator[tuple[AjaxNvr, FakeNvr]]:
    """An AjaxNvr whose relay replays from a fake NVR."""
    fake = FakeNvr()
    server = await asyncio.start_server(fake.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    nvr = AjaxNvr(hass, "127.0.0.1", 8080, "homeassistant", "secret", MAC)
    nvr.client = MagicMock()
    nvr.client.async_update_time_offset = AsyncMock()
    nvr.client.async_get_services = AsyncMock(return_value={"search": "s", "replay": "r"})
    nvr.client.async_find_recordings = AsyncMock(
        return_value=[_recording(f"{TOKEN}-main-r"), _recording(f"{TOKEN}-sub-r")]
    )
    nvr.client.async_get_replay_uri = AsyncMock(
        side_effect=lambda token: f"rtsp://127.0.0.1:{port}/{TOKEN}_r{token[-6]}"
    )
    await nvr.async_initialize()
    yield nvr, fake
    await nvr.async_close()
    server.close()
    await server.wait_closed()


async def _request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, text: str
) -> tuple[str, bytes]:
    writer.write(text.encode())
    head = (await reader.readuntil(b"\r\n\r\n")).decode()
    length = re.search(r"Content-Length: (\d+)", head)
    body = await reader.readexactly(int(length.group(1))) if length else b""
    return head, body


async def test_relay_replays_from_a_moment(nvr: tuple[AjaxNvr, FakeNvr]) -> None:
    """A plain RTSP client gets the recording from the asked time, through the relay."""
    relay, fake = nvr
    start = datetime(2026, 11, 7, 11, 22, 0, tzinfo=UTC)
    url = relay.replay_url(start)
    assert url == f"rtsp://127.0.0.1:{relay.port}/replay/main/20261107T112200Z"
    reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
    try:
        # go2rtc asks for the ONVIF backchannel; the relay asks for a replay instead.
        head, body = await _request(
            reader,
            writer,
            f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\n"
            "Require: www.onvif.org/ver20/backchannel\r\n\r\n",
        )
        assert head.startswith("RTSP/1.0 200 OK") and "CSeq: 1\r\n" in head
        assert f"Content-Base: {url}/" in head
        sdp = body.decode()
        assert "a=recvonly" in sdp and "sendrecv" not in sdp and "m=application" not in sdp
        head, _ = await _request(
            reader,
            writer,
            f"SETUP {url}/streamid=0 RTSP/1.0\r\nCSeq: 2\r\n"
            "Transport: RTP/AVP;unicast;client_port=5000-5001\r\n\r\n",
        )
        assert head.startswith("RTSP/1.0 461")
        head, _ = await _request(
            reader,
            writer,
            f"SETUP {url}/streamid=0 RTSP/1.0\r\nCSeq: 3\r\n"
            "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n",
        )
        assert "Session: 77" in head
        head, _ = await _request(
            reader,
            writer,
            f"PLAY {url} RTSP/1.0\r\nCSeq: 4\r\nSession: 77\r\nRange: npt=0.000-\r\n\r\n",
        )
        assert head.startswith("RTSP/1.0 200 OK")
        assert f"RTP-Info: url={url}/streamid=0" in head
        frames = []
        for _ in range(3):
            assert await reader.readexactly(1) == b"$"
            header = await reader.readexactly(3)
            frames.append(await reader.readexactly(int.from_bytes(header[1:], "big")))
        assert all(frame.endswith(b"frame") for frame in frames)
    finally:
        writer.close()

    nvr_requests = [r for r in fake.requests if "Authorization: Digest" in r]
    assert [r.split(" ", 1)[0] for r in nvr_requests] == ["DESCRIBE", "SETUP", "PLAY"]
    assert all("Require: onvif-replay" in r for r in nvr_requests)
    assert "backchannel" not in "".join(nvr_requests)
    play = nvr_requests[2]
    assert "Range: clock=20261107T112200Z-" in play and "npt" not in play
    assert nvr_requests[1].startswith(
        f"SETUP rtsp://127.0.0.1:{fake_port(nvr)}/{TOKEN}_rm/streamid=0 "
    )


def fake_port(nvr: tuple[AjaxNvr, FakeNvr]) -> int:
    return int(re.search(r":(\d+)/", nvr[0].replay_uris["main"]).group(1))


async def test_unknown_path_is_404(nvr: tuple[AjaxNvr, FakeNvr]) -> None:
    """Only replay URLs are answered."""
    relay, _ = nvr
    reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
    try:
        head, _ = await _request(
            reader,
            writer,
            f"DESCRIBE rtsp://127.0.0.1:{relay.port}/live RTSP/1.0\r\nCSeq: 9\r\n\r\n",
        )
    finally:
        writer.close()
    assert head.startswith("RTSP/1.0 404")


async def test_not_an_nvr(hass: HomeAssistant) -> None:
    """A device without recording services is refused."""
    nvr = AjaxNvr(hass, "127.0.0.1", 8080, "u", "p", MAC)
    nvr.client = MagicMock()
    nvr.client.async_update_time_offset = AsyncMock()
    nvr.client.async_get_services = AsyncMock(return_value={"media2": "m"})
    with pytest.raises(NvrError):
        await nvr.async_initialize()
