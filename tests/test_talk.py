"""Tests for talk-back: the backchannel session and its websocket command."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import re
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.ajax_doorbell.const import DOMAIN
from custom_components.ajax_doorbell.rtsp import RtspError
from custom_components.ajax_doorbell.talk import FRAME_BYTES, TalkSession
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import MAC

# The DoorBell's SDP with the backchannel header (firmware 2.669).
SDP = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Ajax\r\nt=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=1\r\na=recvonly\r\n"
    "m=audio 0 RTP/AVP 9\r\na=rtpmap:9 G722/16000\r\na=control:trackID=2\r\na=recvonly\r\n"
    "m=audio 0 RTP/AVP 9\r\na=rtpmap:9 G722/16000\r\na=control:trackID=3\r\na=sendonly\r\n"
)


class FakeDoorbell:
    """Answers DESCRIBE/SETUP/PLAY/OPTIONS/TEARDOWN and records RTP."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.packets: list[tuple[int, bytes]] = []
        self.got_rtp = asyncio.Event()

    async def wait_packets(self, count: int) -> None:
        async with asyncio.timeout(2):
            while len(self.packets) < count:
                self.got_rtp.clear()
                await self.got_rtp.wait()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":
                    channel, size = struct.unpack(">BH", await reader.readexactly(3))
                    self.packets.append((channel, await reader.readexactly(size)))
                    self.got_rtp.set()
                    continue
                request = (first + await reader.readuntil(b"\r\n\r\n")).decode()
                self.requests.append(request)
                method, uri = request.split(" ", 2)[:2]
                cseq = re.search(r"CSeq: (\d+)", request).group(1)
                if "Authorization" not in request:
                    reply = 'WWW-Authenticate: Digest realm="Ajax RTSP Server", nonce="n1"\r\n'
                    writer.write(
                        f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n{reply}\r\n".encode()
                    )
                    continue
                body = ""
                headers = ""
                if method == "DESCRIBE":
                    body = SDP if "Require: www.onvif.org/ver20/backchannel" in request else ""
                    headers = f"Content-Base: {uri}/\r\nContent-Length: {len(body)}\r\n"
                elif method == "SETUP":
                    headers = (
                        "Session: 4242;timeout=60\r\n"
                        "Transport: RTP/AVP/TCP;unicast;interleaved=4-5\r\n"
                    )
                writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{headers}\r\n{body}".encode())
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def doorbell_server(socket_enabled: None) -> AsyncIterator[tuple[FakeDoorbell, str]]:
    """A fake DoorBell RTSP server and its stream URL."""
    fake = FakeDoorbell()
    server = await asyncio.start_server(fake.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield fake, f"rtsp://127.0.0.1:{port}/9c756e123456-0_m"
    server.close()
    await server.wait_closed()


async def test_session_sends_g722_rtp(doorbell_server: tuple[FakeDoorbell, str]) -> None:
    """Frames become RTP on the backchannel track alone, at the SDP's 16 kHz clock."""
    fake, url = doorbell_server
    session = TalkSession(url, "homeassistant", "secret", "Tablette")
    await session.async_open()
    assert (session.codec, session.sample_rate) == ("G722", 16000)
    session.feed(bytes(range(FRAME_BYTES)) * 2 + b"partial")
    await fake.wait_packets(2)
    await session.async_close()

    methods = [request.split(" ", 1)[0] for request in fake.requests]
    assert methods[:2] == ["DESCRIBE", "DESCRIBE"]  # challenge, then Digest
    assert methods[2:4] == ["SETUP", "PLAY"]
    assert methods[-1] == "TEARDOWN"
    setup = fake.requests[2]
    assert setup.startswith("SETUP rtsp://127.0.0.1:") and "/trackID=3 RTSP/1.0" in setup
    assert "Require: www.onvif.org/ver20/backchannel" in setup
    assert "Session: 4242" in fake.requests[3]

    (channel1, first), (channel2, second) = fake.packets
    assert channel1 == channel2 == 4
    assert first[12:] == bytes(range(FRAME_BYTES))
    v, pt1, seq1, ts1, ssrc1 = struct.unpack(">BBHII", first[:12])
    _, pt2, seq2, ts2, ssrc2 = struct.unpack(">BBHII", second[:12])
    assert v == 0x80 and pt1 == 0x80 | 9 and pt2 == 9  # marker on the first packet only
    assert (seq2 - seq1) & 0xFFFF == 1
    assert (ts2 - ts1) & 0xFFFFFFFF == 320  # 20 ms at 16 kHz
    assert ssrc1 == ssrc2
    assert session.ended == "closed"


async def test_pause_starts_a_new_spurt(doorbell_server: tuple[FakeDoorbell, str]) -> None:
    """After push-to-talk silence the timestamp jumps and the marker is set."""
    fake, url = doorbell_server
    session = TalkSession(url, "homeassistant", "secret", "Téléphone")
    await session.async_open()
    with patch("custom_components.ajax_doorbell.talk.time.monotonic", side_effect=[100.0, 101.0]):
        session.feed(bytes(FRAME_BYTES))
        session.feed(bytes(FRAME_BYTES))
    await fake.wait_packets(2)
    await session.async_close()
    (_, first), (_, second) = fake.packets
    assert second[1] & 0x80
    gap = (struct.unpack(">I", second[4:8])[0] - struct.unpack(">I", first[4:8])[0]) & 0xFFFFFFFF
    assert gap == 16000  # one second of 16 kHz clock


async def test_refused_without_backchannel(socket_enabled: None) -> None:
    """A stream without a backchannel track is refused cleanly."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        cseq = re.search(r"CSeq: (\d+)", request).group(1)
        body = SDP.split("m=audio 0 RTP/AVP 9\r\na=rtpmap:9 G722/16000\r\na=control:trackID=3")[0]
        writer.write(
            f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode()
        )
        await writer.drain()
        await reader.read()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    session = TalkSession(f"rtsp://127.0.0.1:{port}/x", "u", "p", "me")
    with pytest.raises(RtspError, match="no audio backchannel"):
        await session.async_open()
    server.close()
    await server.wait_closed()


@pytest.mark.usefixtures("mock_client", "no_event_stream", "mock_describe")
async def test_websocket_talk(
    hass: HomeAssistant, mock_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """The card gets a binary handler; its frames reach the session; unsubscribing closes it."""
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    camera = er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"{MAC}_camera")
    assert camera

    session = MagicMock()
    session.codec, session.sample_rate = "G722", 16000
    session.async_open = AsyncMock()
    session.async_close = AsyncMock()
    listeners: list = []
    session.add_end_listener = listeners.append
    with patch("custom_components.ajax_doorbell.doorbell.TalkSession", return_value=session):
        client = await hass_ws_client(hass)
        await client.send_json({"id": 5, "type": f"{DOMAIN}/talk", "entity_id": camera})
        result = await client.receive_json()
        assert result["success"], result
        ready = (await client.receive_json())["event"]
        assert ready["type"] == "ready"
        assert (ready["codec"], ready["frame_bytes"]) == ("G722", FRAME_BYTES)

        frame = bytes(range(FRAME_BYTES))
        await client.send_bytes(bytes([ready["handler_id"]]) + frame)
        await client.send_json({"id": 6, "type": "ping"})
        assert (await client.receive_json())["type"] == "pong"
        session.feed.assert_called_once_with(frame)
        assert mock_entry.runtime_data.talk is session

        # Another screen takes the speaker: this one is told.
        for listener in listeners:
            listener("taken_over")
        ended = (await client.receive_json())["event"]
        assert ended == {"type": "ended", "reason": "taken_over"}

        await client.send_json({"id": 7, "type": "unsubscribe_events", "subscription": 5})
        assert (await client.receive_json())["success"]
        await hass.async_block_till_done()
    session.async_close.assert_awaited()
    assert mock_entry.runtime_data.talk is None


@pytest.mark.usefixtures("mock_client", "no_event_stream", "mock_describe")
async def test_websocket_talk_errors(
    hass: HomeAssistant, mock_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """Unknown entities and a refused session are errors, not exceptions."""
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    camera = er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"{MAC}_camera")
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/talk", "entity_id": "camera.other"})
    assert (await client.receive_json())["error"]["code"] == "not_found"

    session = MagicMock()
    session.async_open = AsyncMock(side_effect=RtspError("backchannel SETUP refused (453)"))
    with patch("custom_components.ajax_doorbell.doorbell.TalkSession", return_value=session):
        await client.send_json({"id": 2, "type": f"{DOMAIN}/talk", "entity_id": camera})
        error = (await client.receive_json())["error"]
    assert error["code"] == "talk_failed"
    assert "453" in error["message"]
    assert mock_entry.runtime_data.talk is None


@pytest.mark.usefixtures("mock_client", "no_event_stream", "mock_describe")
async def test_hang_up_closes_the_speaker(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Hanging up on any screen ends the talk."""
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    doorbell = mock_entry.runtime_data
    session = MagicMock()
    session.async_open = AsyncMock()
    session.async_close = AsyncMock()
    with patch("custom_components.ajax_doorbell.doorbell.TalkSession", return_value=session):
        doorbell.call.ring()
        doorbell.call.answer("Tablette")
        assert await doorbell.async_start_talk("Tablette") is session
        doorbell.call.hang_up()
        await hass.async_block_till_done()
    session.async_close.assert_awaited_once_with("hung_up")
    assert doorbell.talk is None
