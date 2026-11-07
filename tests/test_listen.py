"""Tests for listening: the DoorBell's audio relayed to the card."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
import re
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.ajax_doorbell.const import DOMAIN
from custom_components.ajax_doorbell.listen import ListenSession
from custom_components.ajax_doorbell.rtsp import rtp_payload
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import MAC

# The DoorBell's plain SDP: tracks marked sendrecv, G.722 at a 16 kHz clock.
SDP = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Ajax\r\nt=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=1\r\na=sendrecv\r\n"
    "m=audio 0 RTP/AVP 9\r\na=rtpmap:9 G722/16000\r\na=control:trackID=2\r\na=sendrecv\r\n"
)


def _rtp(payload: bytes, *, extension: bool = False, padding: int = 0) -> bytes:
    first = 0x80 | (0x10 if extension else 0) | (0x20 if padding else 0)
    packet = struct.pack(">BBHII", first, 9, 1, 960, 7)
    if extension:
        packet += struct.pack(">HH", 0xABAC, 1) + b"\x00" * 4
    packet += payload
    if padding:
        packet += b"\x00" * (padding - 1) + bytes([padding])
    return packet


def test_rtp_payload() -> None:
    """Headers, extensions and padding are removed."""
    assert rtp_payload(_rtp(b"voice")) == b"voice"
    assert rtp_payload(_rtp(b"voice", extension=True)) == b"voice"
    assert rtp_payload(_rtp(b"voice", padding=3)) == b"voice"
    assert rtp_payload(b"\x00" * 8) is None


class FakeDoorbell:
    """Serves the audio track on the channel it picks, with video noise on another."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request = (await reader.readuntil(b"\r\n\r\n")).decode()
                self.requests.append(request)
                method, uri = request.split(" ", 2)[:2]
                cseq = re.search(r"CSeq: (\d+)", request).group(1)
                if "Authorization" not in request:
                    challenge = 'WWW-Authenticate: Digest realm="Ajax RTSP Server", nonce="n"\r\n'
                    writer.write(
                        f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\n{challenge}\r\n".encode()
                    )
                    continue
                headers, body = "", ""
                if method == "DESCRIBE":
                    body = SDP
                    headers = f"Content-Base: {uri}/\r\nContent-Length: {len(body)}\r\n"
                elif method == "SETUP":
                    headers = "Session: 5;timeout=60\r\nTransport: RTP/AVP/TCP;unicast;interleaved=2-3\r\n"
                writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{headers}\r\n{body}".encode())
                if method == "PLAY":
                    for channel, packet in (
                        (0, _rtp(b"video")),
                        (2, _rtp(b"\x01\x02", extension=True)),
                        (2, _rtp(b"\x03\x04")),
                    ):
                        writer.write(
                            b"$" + bytes([channel]) + len(packet).to_bytes(2, "big") + packet
                        )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def doorbell_server(socket_enabled: None) -> AsyncIterator[tuple[FakeDoorbell, str]]:
    """A fake DoorBell and its stream URL."""
    fake = FakeDoorbell()
    server = await asyncio.start_server(fake.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield fake, f"rtsp://127.0.0.1:{port}/9c756e123456-0_m"
    server.close()
    await server.wait_closed()


async def test_session_hands_over_audio(doorbell_server: tuple[FakeDoorbell, str]) -> None:
    """Only the audio track is set up; its payloads reach the listeners."""
    fake, url = doorbell_server
    session = ListenSession(url, "homeassistant", "secret")
    await session.async_open()
    assert (session.codec, session.sample_rate) == ("G722", 16000)
    received: list[bytes] = []
    got = asyncio.Event()

    def listener(payload: bytes) -> None:
        received.append(payload)
        if len(received) == 2:
            got.set()

    session.add_listener(listener)
    async with asyncio.timeout(2):
        await got.wait()
    await session.async_close()
    assert received == [b"\x01\x02", b"\x03\x04"]  # channel 2 only, headers removed
    setups = [r for r in fake.requests if r.startswith("SETUP")]
    assert len(setups) == 1 and "/trackID=2 RTSP/1.0" in setups[0]
    assert "Require" not in "".join(fake.requests)  # a plain viewer session
    assert session.ended == "closed"


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.codec, session.sample_rate, session.ended = "G722", 16000, None
    session.async_open = AsyncMock()
    session.async_close = AsyncMock()
    session.listeners_list = []

    def add_listener(listener):
        session.listeners_list.append(listener)
        return lambda: session.listeners_list.remove(listener)

    session.add_listener = add_listener
    type(session).listeners = property(lambda self: len(self.listeners_list))
    session.add_end_listener = MagicMock(return_value=lambda: None)
    return session


@pytest.mark.usefixtures("mock_client", "no_event_stream", "mock_describe")
async def test_websocket_listen(
    hass: HomeAssistant, mock_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """Two screens share one session; it closes a little after the last one leaves."""
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    camera = er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"{MAC}_camera")
    session = _fake_session()
    with patch(
        "custom_components.ajax_doorbell.doorbell.ListenSession", return_value=session
    ) as created:
        first = await hass_ws_client(hass)
        second = await hass_ws_client(hass)
        for client in (first, second):
            await client.send_json({"id": 1, "type": f"{DOMAIN}/listen", "entity_id": camera})
            assert (await client.receive_json())["success"]
            ready = (await client.receive_json())["event"]
            assert ready == {"type": "ready", "codec": "G722", "sample_rate": 16000}
        assert created.call_count == 1, "one shared session"
        for listener in session.listeners_list:
            listener(b"\x01\x02")
        assert (await first.receive_json())["event"] == {"a": "AQI="}
        assert (await second.receive_json())["event"] == {"a": "AQI="}

        for client in (first, second):
            await client.send_json({"id": 2, "type": "unsubscribe_events", "subscription": 1})
            assert (await client.receive_json())["success"]
        await hass.async_block_till_done()
        session.async_close.assert_not_awaited()  # kept for a moment
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
        session.async_close.assert_awaited_once()


@pytest.mark.usefixtures("mock_client", "no_event_stream", "mock_describe")
async def test_listen_replay_needs_the_nvr(
    hass: HomeAssistant, mock_entry: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """Replay audio without an NVR is a clean error; the call sensor advertises the relay."""
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    camera = er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"{MAC}_camera")
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/listen",
            "entity_id": camera,
            "start": dt_util.utcnow().isoformat(),
        }
    )
    assert (await client.receive_json())["error"]["code"] == "listen_failed"
    call = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{MAC}_call_state")
    assert hass.states.get(call).attributes["listen_via"] == "relay"
