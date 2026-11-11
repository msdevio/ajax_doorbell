"""Tests for what the camera card uses: journal, replays, spans, pictures, options."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator

from custom_components.ajax_doorbell.const import DOMAIN
from custom_components.ajax_doorbell.hub import to_segments
from custom_components.ajax_doorbell.journal import DetectionLog
from custom_components.ajax_doorbell.onvif import OnvifEvent
from homeassistant.components.camera import async_get_stream_source
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .test_hub import MOTION, NVR_DATA, NVR_MAC, OBJECT, TURRET, make_nvr_client

T0 = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# -------------------------------------------------------------------- journal


async def test_journal_merges_and_prunes(hass: HomeAssistant) -> None:
    """Activity close together is one detection; old ones go; the list is stored."""
    log = DetectionLog(hass, "entry", 2)
    seen: list[str] = []
    log.async_add_listener(seen.append)
    first = log.add("cam", "motion", _at(0))
    assert log.add("cam", "person", _at(0.3)) is first  # 18 s later: the same
    log.extend("cam", _at(0.5))
    assert (first.end, first.kinds) == (_at(0.5), {"motion", "person"})
    second = log.add("cam", "motion", _at(5))  # minutes later: another one
    assert second is not first
    log.extend("cam", _at(20))  # too late to extend anything
    assert second.end == _at(5)
    log.add("cam", "motion", _at(3 * 24 * 60))  # three days on: the old ones go
    assert [d.start for d in log.cameras["cam"]] == [_at(3 * 24 * 60)]
    assert seen == ["cam"] * 5
    assert log.get("cam", log.cameras["cam"][0].id) is log.cameras["cam"][0]

    await log._store.async_save(log._data())
    again = DetectionLog(hass, "entry", 2)
    with patch(
        "custom_components.ajax_doorbell.journal.dt_util.utcnow",
        return_value=_at(3 * 24 * 60),
    ):
        await again.async_load()
    assert [d.kinds for d in again.cameras["cam"]] == [{"motion"}]


def test_to_segments() -> None:
    """The NVR's changes, repeated per track, become spans of video."""
    changes = [
        (_at(0), False),
        (_at(0), False),
        (_at(2), True),
        (_at(2), True),
        (_at(3), False),
        (_at(3), False),
        (_at(10), True),
    ]
    assert to_segments(changes, _at(1), _at(20)) == [(_at(2), _at(3)), (_at(10), _at(20))]
    # A recording already running when the range starts
    assert to_segments([(_at(-5), True)], _at(0), _at(4)) == [(_at(0), _at(4))]
    assert to_segments([], _at(0), _at(4)) == []


# ---------------------------------------------------------------- the NVR set up


@pytest.fixture
async def nvr(
    hass: HomeAssistant, no_event_stream: None, socket_enabled: None
) -> AsyncIterator[tuple[MockConfigEntry, MagicMock]]:
    """The NVR entry with its cameras."""
    client = make_nvr_client()
    entry = MockConfigEntry(domain=DOMAIN, title="Ajax NVR", unique_id=NVR_MAC, data=dict(NVR_DATA))
    entry.add_to_hass(hass)
    with patch("custom_components.ajax_doorbell.hub.AjaxOnvifClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, client


def _camera(hass: HomeAssistant, key: str = "camera") -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "camera", DOMAIN, f"{NVR_MAC}_{TURRET}_{key}"
    )
    assert entity_id
    return entity_id


async def test_detections_are_journaled(
    hass: HomeAssistant, nvr: tuple[MockConfigEntry, MagicMock]
) -> None:
    """Rising detections start a journal line; the end of activity extends it."""
    hub = nvr[0].runtime_data
    source = {"VideoSourceToken": f"{TURRET}-v", "Rule": "r"}

    def event(topic: str, data: dict[str, str], operation: str = "Changed") -> None:
        hub._on_event(OnvifEvent(topic, operation, None, source, data))

    event(MOTION, {"Detected": "true"}, "Initialized")  # the state at subscription: not new
    assert hub.journal.cameras.get(TURRET, []) == []
    event(MOTION, {"Detected": "false"})
    event(MOTION, {"Detected": "true"})
    event(OBJECT, {"ClassTypes": "Human"})
    event(MOTION, {"Detected": "true"})  # repeated state: nothing new
    event(MOTION, {"Detected": "false"})
    detections = hub.journal.cameras[TURRET]
    assert len(detections) == 1
    assert detections[0].kinds == {"motion", "person"}
    assert detections[0].end >= detections[0].start


async def test_replay_camera(hass: HomeAssistant, nvr: tuple[MockConfigEntry, MagicMock]) -> None:
    """A hidden replay camera per recorded camera, pointed at the chosen moment."""
    hub = nvr[0].runtime_data
    replay = _camera(hass, "replay")
    assert er.async_get(hass).async_get(replay).hidden_by is er.RegistryEntryHider.INTEGRATION
    channel = hub.channels[TURRET]
    assert channel.replay_start is not None  # a default moment, so it can stream at once
    hub.set_replay(channel, _at(0))
    await hass.async_block_till_done()
    assert await async_get_stream_source(hass, replay) == (
        f"rtsp://127.0.0.1:{hub.port}/replay/{TURRET}/main/20261112T100000Z"
    )
    assert hass.states.get(replay).attributes["replay_start"] == _at(0).isoformat()
    with patch(
        "custom_components.ajax_doorbell.hub._grab_frame", AsyncMock(return_value=b"jpg")
    ) as grab:
        assert await hub.async_thumbnail(channel, _at(0)) == b"jpg"
        assert await hub.async_thumbnail(channel, _at(0)) == b"jpg"  # kept in memory
    assert grab.await_count == 1
    assert grab.await_args.args[0].endswith(f"/replay/{TURRET}/sub/20261112T100000Z")


async def test_segments_are_cached(
    hass: HomeAssistant, nvr: tuple[MockConfigEntry, MagicMock]
) -> None:
    """Spans come from the NVR's history, asked again only after a while."""
    hub, client = nvr[0].runtime_data, nvr[1]
    client.async_find_recording_history.return_value = [(_at(1), True), (_at(2), False)]
    channel = hub.channels[TURRET]
    assert await hub.async_segments(channel, _at(0), _at(10)) == [(_at(1), _at(2))]
    assert await hub.async_segments(channel, _at(0), _at(10)) == [(_at(1), _at(2))]
    client.async_find_recording_history.assert_awaited_once_with(
        f"{TURRET}-main-r", _at(0), _at(10)
    )


# --------------------------------------------------------------- websocket API


async def test_card_api(
    hass: HomeAssistant,
    nvr: tuple[MockConfigEntry, MagicMock],
    hass_ws_client: WebSocketGenerator,
    hass_client: ClientSessionGenerator,
) -> None:
    """The card follows a camera, reads its spans, replays and shows pictures."""
    hub, client = nvr[0].runtime_data, nvr[1]
    camera = _camera(hass)
    hub.journal.add(TURRET, "person", dt_util.utcnow() - timedelta(minutes=3))
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/nvr_camera", "entity_id": camera})
    assert (await ws.receive_json())["success"]
    info = (await ws.receive_json())["event"]
    assert info["name"] == "Jardin"
    assert info["recorded"] is True
    assert info["replay_camera"] == _camera(hass, "replay")
    assert info["retention_days"] == 2
    (detection,) = info["detections"]
    assert detection["kinds"] == ["person"]
    assert detection["thumb"].startswith(f"/api/{DOMAIN}/nvr_thumb/{nvr[0].entry_id}/{TURRET}/")
    assert "authSig=" in detection["thumb"]

    # A new detection reaches the card (grouped, a second later)
    hub.journal.add(TURRET, "motion")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    info = (await ws.receive_json())["event"]
    assert len(info["detections"]) == 2

    client.async_find_recording_history.return_value = [(_at(1), True), (_at(2), False)]
    await ws.send_json(
        {
            "id": 2,
            "type": f"{DOMAIN}/nvr_segments",
            "entity_id": camera,
            "start": _at(0).isoformat(),
            "end": _at(60).isoformat(),
        }
    )
    reply = await ws.receive_json()
    assert reply["result"] == {"segments": [[_at(1).isoformat(), _at(2).isoformat()]]}

    past = dt_util.utcnow().replace(microsecond=0) - timedelta(hours=1)
    await ws.send_json(
        {"id": 3, "type": f"{DOMAIN}/nvr_replay", "entity_id": camera, "start": past.isoformat()}
    )
    reply = await ws.receive_json()
    assert reply["result"] == {"camera": _camera(hass, "replay"), "start": past.isoformat()}
    assert hub.channels[TURRET].replay_start == past
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    info = (await ws.receive_json())["event"]
    assert info["replay_start"] == past.isoformat()

    # Not a camera of the NVR
    await ws.send_json(
        {
            "id": 4,
            "type": f"{DOMAIN}/nvr_replay",
            "entity_id": "camera.nope",
            "start": _at(1).isoformat(),
        }
    )
    assert (await ws.receive_json())["error"]["code"] == "not_found"

    # The picture, through its signed path
    http = await hass_client()
    with patch.object(hub, "async_thumbnail", AsyncMock(return_value=b"\xff\xd8jpeg")):
        response = await http.get(detection["thumb"])
        assert response.status == 200
        assert await response.read() == b"\xff\xd8jpeg"
        response = await http.get(f"/api/{DOMAIN}/nvr_thumb/{nvr[0].entry_id}/{TURRET}/notatime")
        assert response.status == 404


# ---------------------------------------------------------------- the relay


SDP = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nt=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\na=control:streamid=0\r\na=sendrecv\r\n"
    "m=application 0 RTP/AVP 107\r\na=control:streamid=2\r\na=sendrecv\r\n"
)


class FakeReplayNvr:
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
                headers, body = "", ""
                if method == "DESCRIBE":
                    body = SDP
                    headers = f"Content-Base: {uri}/\r\nContent-Length: {len(body)}\r\n"
                elif method == "SETUP":
                    transport = re.search(r"Transport: ([^\r]*)", request).group(1)
                    headers = f"Session: 77;timeout=30\r\nTransport: {transport}\r\n"
                writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{headers}\r\n{body}".encode())
                if method == "PLAY":
                    rtp = bytes([0x80, 96, 0, 1]) + bytes(8) + b"recorded"
                    writer.write(b"$\x00" + len(rtp).to_bytes(2, "big") + rtp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def _request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, text: str
) -> tuple[str, bytes]:
    writer.write(text.encode())
    head = (await reader.readuntil(b"\r\n\r\n")).decode()
    length = re.search(r"Content-Length: (\d+)", head)
    body = await reader.readexactly(int(length.group(1))) if length else b""
    return head, body


async def test_replay_relay(
    hass: HomeAssistant, no_event_stream: None, socket_enabled: None
) -> None:
    """The relay plays a recording from the moment in its path."""
    fake = FakeReplayNvr()
    server = await asyncio.start_server(fake.serve, "127.0.0.1", 0)
    client = make_nvr_client("127.0.0.1", server.sockets[0].getsockname()[1])
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=NVR_MAC, data={**NVR_DATA, CONF_HOST: "127.0.0.1"}
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ajax_doorbell.hub.AjaxOnvifClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    hub = entry.runtime_data
    local = hub.replay_url(hub.channels[TURRET], _at(0))
    reader, writer = await asyncio.open_connection("127.0.0.1", hub.port)
    try:
        head, body = await _request(reader, writer, f"DESCRIBE {local} RTSP/1.0\r\nCSeq: 1\r\n\r\n")
        assert head.startswith("RTSP/1.0 200")
        assert body.decode().count("a=recvonly") == 1  # the metadata track is left out
        head, _ = await _request(
            reader,
            writer,
            f"SETUP {local}/streamid=0 RTSP/1.0\r\nCSeq: 2\r\n"
            "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n",
        )
        assert head.startswith("RTSP/1.0 200")
        head, _ = await _request(
            reader, writer, f"PLAY {local} RTSP/1.0\r\nCSeq: 3\r\nRange: npt=0.000-\r\n\r\n"
        )
        assert head.startswith("RTSP/1.0 200")
        frame = await asyncio.wait_for(reader.readexactly(4 + 20), 2)
        assert frame.endswith(b"recorded")
    finally:
        writer.close()
        assert await hass.config_entries.async_unload(entry.entry_id)
        server.close()
        await server.wait_closed()
    play = next(r for r in fake.requests if r.startswith("PLAY") and "Digest" in r)
    assert "Require: onvif-replay" in play
    assert "Range: clock=20261112T100000Z-" in play
    assert f"/{TURRET}_rm" in play.split(" ", 2)[1]


# --------------------------------------------------------------------- options


async def test_nvr_options(hass: HomeAssistant, nvr: tuple[MockConfigEntry, MagicMock]) -> None:
    """The NVR's options set how long detections stay listed; the NVR reloads."""
    entry, _ = nvr
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "nvr"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"detection_retention": 7}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {"detection_retention": 7}
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.journal.retention == timedelta(days=7)


async def test_removing_the_nvr_forgets_its_journal(
    hass: HomeAssistant, nvr: tuple[MockConfigEntry, MagicMock], hass_storage: dict
) -> None:
    """The detections' times go with the NVR's entry."""
    entry, _ = nvr
    hub = entry.runtime_data
    hub.journal.add(TURRET, "motion")
    await hub.journal._store.async_save(hub.journal._data())
    key = f"{DOMAIN}.{entry.entry_id}.detections"
    assert key in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
