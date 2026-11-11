"""Tests for the NVR entry: its cameras, their events, the live relay, recording modes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta
import json
import re
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ajax_doorbell.const import (
    DOMAIN,
    MODE_CLIPS,
    MODE_CONTINUOUS,
    MODE_NOT_RECORDED,
    MODE_UNKNOWN,
)
from custom_components.ajax_doorbell.diagnostics import async_get_config_entry_diagnostics
from custom_components.ajax_doorbell.hub import (
    NTP_EPOCH,
    channel_group,
    channel_mac,
    channel_name,
    pick_names,
    probe_replay,
    recording_mode,
)
from custom_components.ajax_doorbell.onvif import (
    DeviceIdentity,
    MediaProfile,
    OnvifEvent,
    ProfileDetails,
    Recording,
)
from homeassistant.components.camera import async_get_stream_source
from homeassistant.config_entries import SOURCE_DHCP, SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.util import dt as dt_util

from .conftest import MAC

NVR_HOST = "192.168.128.30"
NVR_MAC = "9c:75:6e:2e:77:cf"
TURRET_MAC = "9c756eabcdef"
BELL = f"bcFhIz1NqO-{MAC.replace(':', '')}-0"  # the DoorBell's channel on the NVR
TURRET = f"bcFhIz1NqO-{TURRET_MAC}-0"
NVR_DATA = {
    CONF_HOST: NVR_HOST,
    CONF_PORT: 8080,
    CONF_USERNAME: "homeassistant",
    CONF_PASSWORD: "secret",
    "kind": "nvr",
}
MOTION = "tns1:RuleEngine/tnsajax:MotionDetector/Detection"
OBJECT = "tns1:RuleEngine/ObjectDetection/Object"
TRACKING = "tns1:RuleEngine/tnsajax:ObjectDetector/Detection"
JOB = "tns1:RecordingConfig/JobState"
TOPICS = ["tns1:RuleEngine/tnsajax:RingDetector/Detection", MOTION, OBJECT, TRACKING, JOB]


def _profiles() -> list[MediaProfile]:
    return [
        MediaProfile(f"{BELL}-main", "Sonnette _main", "H264", 2688, 1520, 25.0),
        MediaProfile(f"{BELL}-sub", "Sonnette _sub", "H264", 1024, 576, 25.0),
        MediaProfile(f"{TURRET}-sub", "Jardin _sub", "H264", 1024, 576, 20.0),
        MediaProfile(f"{TURRET}-main", "Jardin _main", "H264", 2560, 1440, 20.0),
    ]


def make_nvr_client(host: str = NVR_HOST, rtsp_port: int = 8554) -> MagicMock:
    """Return an ONVIF client double that answers like the NVR."""
    client = MagicMock()
    client.host = host
    client.services = {"device": "d", "search": "s", "replay": "r", "recording": "c"}
    client.uses_http_digest = False
    client.time_offset = timedelta(0)
    client.async_update_time_offset = AsyncMock(return_value=timedelta(0))
    client.async_get_services = AsyncMock(return_value=client.services)
    client.async_get_identity = AsyncMock(
        return_value=DeviceIdentity("AJAX", "sav704q-nand", "2.803-1", "310B41E0")
    )
    client.async_get_mac = AsyncMock(return_value=NVR_MAC)
    client.async_get_profiles = AsyncMock(return_value=_profiles())
    client.async_get_profile_details = AsyncMock(
        return_value={
            p.token: ProfileDetails(p.name, frozenset({f"{channel_group(p.token)}-v"}), "")
            for p in _profiles()
        }
    )
    # The NVR announces another of its addresses: streams are aimed at the configured one.
    client.async_get_stream_uri = AsyncMock(
        side_effect=lambda token: (
            f"rtsp://10.9.9.9:{rtsp_port}/{channel_group(token)}_{'s' if 'sub' in token else 'm'}"
        )
    )
    client.async_find_recordings = AsyncMock(
        return_value=[
            Recording(f"{group}-{quality}-r", name, datetime(2026, 11, 1, tzinfo=UTC), None)
            for group, name in ((BELL, "Sonnette"), (TURRET, "Jardin"))
            for quality in ("main", "sub")
        ]
    )
    client.async_get_recording_jobs = AsyncMock(return_value={"job_7": f"{TURRET}-main-r"})
    client.async_get_topics = AsyncMock(return_value=TOPICS)
    client.async_get_replay_uri = AsyncMock(
        side_effect=lambda token: f"rtsp://10.9.9.9:{rtsp_port}/{channel_group(token)}_rm"
    )
    client.async_find_recording_history = AsyncMock(return_value=[])
    return client


@pytest.fixture
def nvr_client() -> Generator[MagicMock]:
    """Replace the NVR's ONVIF client."""
    client = make_nvr_client()
    with patch("custom_components.ajax_doorbell.hub.AjaxOnvifClient", return_value=client):
        yield client


@pytest.fixture
def nvr_entry() -> MockConfigEntry:
    """Return the NVR's config entry."""
    return MockConfigEntry(domain=DOMAIN, title="Ajax NVR", unique_id=NVR_MAC, data=dict(NVR_DATA))


@pytest.fixture
async def both(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_describe: AsyncMock,
    nvr_client: MagicMock,
    nvr_entry: MockConfigEntry,
    mock_entry: MockConfigEntry,
    no_event_stream: None,
    socket_enabled: None,  # the live relay listens on 127.0.0.1
) -> MockConfigEntry:
    """The DoorBell and the NVR, both set up."""
    mock_entry.add_to_hass(hass)
    nvr_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(nvr_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.LOADED
    assert nvr_entry.state is ConfigEntryState.LOADED
    return nvr_entry


def _entity(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{NVR_MAC}_{key}")
    assert entity_id, key
    return entity_id


def test_tokens() -> None:
    """Channel, MAC and name come out of Ajax's tokens."""
    assert channel_group(f"{TURRET}-main") == TURRET
    assert channel_group(f"{TURRET}-sub-r") == TURRET
    assert channel_mac(TURRET) == "9c:75:6e:ab:cd:ef"
    assert channel_mac("camera_1") is None
    assert channel_name("Jardin _main") == "Jardin"
    assert channel_name("Garage_sub") == "Garage"


def test_pick_names() -> None:
    """Names come from the first source that tells the cameras apart."""
    assert pick_names({"a": {"profile": "Jardin"}, "b": {"profile": "Garage"}}) == {
        "a": "Jardin",
        "b": "Garage",
    }
    # The NVR kept one camera's old name in its profiles; the recordings have both
    stale = {
        "a": {"profile": "camera porte", "recording": "camera porte", "fallback": "x"},
        "b": {"profile": "Camera Porte", "recording": "camera garage", "fallback": "x"},
    }
    assert pick_names(stale) == {"a": "camera porte", "b": "camera garage"}
    # Nothing tells them apart: numbered
    same = {
        "a": {"profile": "camera porte", "recording": "camera porte"},
        "b": {"profile": "camera porte", "recording": ""},
        "c": {"profile": "", "recording": ""},
    }
    assert pick_names(same) == {"a": "camera porte", "b": "camera porte 2", "c": "Camera 3"}
    assert pick_names({"a": {"profile": "", "recording": "Jardin"}}) == {"a": "Jardin"}


@pytest.mark.usefixtures("no_event_stream", "socket_enabled")
async def test_cameras_named_by_their_recordings(
    hass: HomeAssistant, nvr_client: MagicMock, nvr_entry: MockConfigEntry
) -> None:
    """Two cameras the profiles name alike get their recordings' names."""
    other = "HAjEnJZbcI-9c756e111111-0"
    profiles = [p for p in _profiles() if p.token.startswith(TURRET)]
    profiles += [
        MediaProfile(f"{other}-main", "Jardin _main", "H265", 3840, 2160, 20.0),
        MediaProfile(f"{other}-sub", "Jardin _sub", "H265", 720, 576, 20.0),
    ]
    nvr_client.async_get_profiles.return_value = profiles
    nvr_client.async_get_profile_details.return_value = {
        p.token: ProfileDetails(p.name, frozenset(), "") for p in profiles
    }
    nvr_client.async_find_recordings.return_value = [
        Recording(f"{TURRET}-main-r", "Jardin", None, None),
        Recording(f"{other}-main-r", "Garage", None, None),
    ]
    nvr_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(nvr_entry.entry_id)
    await hass.async_block_till_done()
    hub = nvr_entry.runtime_data
    assert {c.name for c in hub.channels.values()} == {"Jardin", "Garage"}
    assert hub.channels[other].names["profile"] == "Jardin"
    devices = dr.async_get(hass)
    garage = devices.async_get_device_by_identifier(
        (DOMAIN, f"{NVR_MAC}_{other}"), nvr_entry.entry_id
    )
    assert garage is not None
    assert garage.name == "Garage"


async def test_cameras_get_devices(
    hass: HomeAssistant, both: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """Each camera of the NVR gets a device; the DoorBell's channel is left to the DoorBell."""
    # HA 2026 rejects entities whose device uses the deprecated via_device when it
    # cannot tell which integration asked: the cameras link to the NVR by device id.
    assert "via_device" not in caplog.text
    assert "Error adding entity" not in caplog.text
    hub = both.runtime_data
    assert "via_device" not in dict(hub.channel_device_info(hub.channels[TURRET]))
    devices = dr.async_get(hass)
    nvr = devices.async_get_device_by_identifier((DOMAIN, NVR_MAC), both.entry_id)
    assert nvr is not None
    assert (nvr.model, nvr.sw_version, nvr.name) == ("NVR", "2.803-1", "Ajax NVR")
    turret = devices.async_get_device_by_identifier((DOMAIN, f"{NVR_MAC}_{TURRET}"), both.entry_id)
    assert turret is not None
    assert turret.name == "Jardin"
    assert turret.via_device_id == nvr.id
    assert not devices.async_get_devices(identifiers={(DOMAIN, f"{NVR_MAC}_{BELL}")})
    assert len(dr.async_entries_for_config_entry(devices, both.entry_id)) == 2

    hub = both.runtime_data
    assert hub.skipped == {BELL: "doorbell"}
    channel = hub.channels[TURRET]
    assert channel.stream_uris == {
        "main": f"rtsp://{NVR_HOST}:8554/{TURRET}_m",
        "sub": f"rtsp://{NVR_HOST}:8554/{TURRET}_s",
    }
    camera = _entity(hass, "camera", f"{TURRET}_camera")
    assert await async_get_stream_source(hass, camera) == (
        f"rtsp://127.0.0.1:{hub.port}/live/{TURRET}/main"
    )
    sub = er.async_get(hass).async_get(_entity(hass, "camera", f"{TURRET}_camera_sub"))
    assert sub.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(camera).attributes["resolution"] == "2560x1440"
    # Only what the entities use: no ring, no tracking flood (standard objects exist)
    assert hub.events.topics == [MOTION, OBJECT, JOB]
    for key in ("motion", "person", "vehicle", "pet"):
        assert hass.states.get(_entity(hass, "binary_sensor", f"{TURRET}_{key}")).state == STATE_OFF
    assert hass.states.get(_entity(hass, "binary_sensor", f"{TURRET}_recording")).state == (
        STATE_UNKNOWN
    )
    assert hass.states.get(_entity(hass, "binary_sensor", "event_stream")).state == STATE_ON
    assert hass.states.get(_entity(hass, "sensor", f"{TURRET}_recording_mode")).state == (
        MODE_UNKNOWN
    )


async def test_doorbell_unchanged_next_to_the_nvr(
    hass: HomeAssistant, both: MockConfigEntry, mock_entry: MockConfigEntry
) -> None:
    """The call services still reach every DoorBell, the NVR is left out."""
    await hass.services.async_call(DOMAIN, "dismiss", {}, blocking=True)
    await hass.services.async_call(DOMAIN, "clear_missed", {}, blocking=True)
    ring = er.async_get(hass).async_get_entity_id("event", DOMAIN, f"{MAC}_ring")
    assert ring is not None
    assert er.async_get(hass).async_get(ring).config_entry_id == mock_entry.entry_id


async def test_events_reach_their_camera(hass: HomeAssistant, both: MockConfigEntry) -> None:
    """Motion, people and the recording job, each on its camera's entities."""
    hub = both.runtime_data
    source = {"VideoSourceToken": f"{TURRET}-v", "Rule": "r1"}

    def event(topic: str, data: dict[str, str], source: dict[str, str] = source) -> None:
        hub._on_event(OnvifEvent(topic, "Changed", None, source, data))

    event(MOTION, {"Detected": "true"})
    event(OBJECT, {"ClassTypes": "Human"})
    event(TRACKING, {})  # tracking boxes: ignored
    # A job token without the camera's name: matched through the recording jobs
    event(JOB, {"State": "Active"}, {"RecordingJobToken": "job_7"})
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "binary_sensor", f"{TURRET}_motion")).state == STATE_ON
    assert hass.states.get(_entity(hass, "binary_sensor", f"{TURRET}_person")).state == STATE_ON
    assert hass.states.get(_entity(hass, "binary_sensor", f"{TURRET}_recording")).state == STATE_ON

    event(OBJECT, {"ClassTypes": ""})
    event(JOB, {"State": "Idle"}, {"RecordingJobToken": "job_7"})
    await hass.async_block_till_done()
    assert hass.states.get(_entity(hass, "binary_sensor", f"{TURRET}_person")).state == STATE_OFF
    assert hass.states.get(_entity(hass, "binary_sensor", f"{TURRET}_recording")).state == (
        STATE_OFF
    )

    # The DoorBell's own events on the NVR are its entry's business; strangers are counted.
    event(
        "tns1:RuleEngine/tnsajax:RingDetector/Detection",
        {"Detected": "true"},
        {"VideoSourceToken": f"{BELL}-v"},
    )
    event(MOTION, {"Detected": "true"}, {"VideoSourceToken": "elsewhere"})
    assert hub.unrouted == {"RuleEngine/MotionDetector/Detection": 1}
    assert len(hub.channels[TURRET].last_events) == 5


def _probe(delta: float | None = None, error: str | None = None) -> dict:
    result = {"asked": "2026-11-11T10:00:00+00:00", "plays": None, "delta_s": delta}
    if delta is not None:
        result["plays"] = "2026-11-11T10:00:01+00:00"
    if error:
        result["error"] = error
    return result


def test_recording_mode() -> None:
    """Replays tell continuous recording from clips; the NVR's history helps."""
    assert recording_mode([_probe(0.4), _probe(-0.3), _probe(1.0)], None) == MODE_CONTINUOUS
    assert recording_mode([_probe(0.4), _probe(1490)], None) == MODE_CLIPS
    assert recording_mode([_probe(0.4), _probe()], None) == MODE_CLIPS  # nothing there
    assert recording_mode([_probe(error="PLAY refused (454)")], None) == MODE_UNKNOWN
    assert recording_mode([_probe()], None) == MODE_UNKNOWN  # no replay works at all
    moments = [datetime(2026, 11, 11, h, tzinfo=UTC) for h in range(4)]
    history = [(moments[0], True), (moments[1], False), (moments[2], True), (moments[3], False)]
    assert recording_mode([], history) == MODE_CLIPS
    assert recording_mode([], [(moments[0], True)]) == MODE_UNKNOWN


async def test_analysis_sets_the_mode(
    hass: HomeAssistant, both: MockConfigEntry, nvr_client: MagicMock
) -> None:
    """Shortly after setup, the replays show the camera records continuously."""

    async def replay(uri: str, asked: datetime, *args) -> dict:
        assert uri == f"rtsp://{NVR_HOST}:8554/{TURRET}_rm"
        return {"asked": asked.isoformat(), "plays": asked.isoformat(), "delta_s": 0.3}

    with patch("custom_components.ajax_doorbell.hub.probe_replay", side_effect=replay):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()
    sensor = hass.states.get(_entity(hass, "sensor", f"{TURRET}_recording_mode"))
    assert sensor.state == MODE_CONTINUOUS
    assert len(sensor.attributes["replays"]) == 4
    nvr_client.async_get_replay_uri.assert_awaited_once_with(f"{TURRET}-main-r")
    nvr_client.async_find_recording_history.assert_awaited_once()

    # A camera the NVR does not record
    hub = both.runtime_data
    hub.channels[TURRET].recordings.clear()
    await hub.async_analyze()
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).state == MODE_NOT_RECORDED


async def test_diagnostics_hide_the_cameras(hass: HomeAssistant, both: MockConfigEntry) -> None:
    """No password and no camera MAC address in the NVR's diagnostics."""
    text = json.dumps(await async_get_config_entry_diagnostics(hass, both))
    assert "secret" not in text
    assert TURRET_MAC not in text
    assert MAC.replace(":", "") not in text
    assert '"name": "Jardin"' in text


async def test_unload(hass: HomeAssistant, both: MockConfigEntry) -> None:
    """Unloading the NVR closes its relay and leaves the DoorBell alone."""
    hub = both.runtime_data
    assert await hass.config_entries.async_unload(both.entry_id)
    await hass.async_block_till_done()
    assert both.state is ConfigEntryState.NOT_LOADED
    assert hub._server is None


# ---------------------------------------------------------------- live relay

LIVE_SDP = (
    "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Ajax\r\nt=0 0\r\n"
    "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:{base}/trackID=1\r\n"
    "a=sendrecv\r\nm=audio 0 RTP/AVP 9\r\na=rtpmap:9 G722/16000\r\n"
    "a=control:{base}/trackID=2\r\na=sendrecv\r\n"
)


class FakeLiveNvr:
    """Streams a camera live; wants Digest auth; marks its tracks sendrecv."""

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
                    body = LIVE_SDP.format(base=uri)
                    headers = f"Content-Base: {uri}/\r\nContent-Length: {len(body)}\r\n"
                elif method == "SETUP":
                    transport = re.search(r"Transport: ([^\r]*)", request).group(1)
                    headers = f"Session: 5;timeout=60\r\nTransport: {transport}\r\n"
                writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{headers}\r\n{body}".encode())
                if method == "PLAY":
                    for n in range(3):
                        rtp = bytes([0x80, 96, 0, n]) + bytes(8) + b"live"
                        writer.write(b"$\x00" + len(rtp).to_bytes(2, "big") + rtp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def live(
    hass: HomeAssistant, socket_enabled: None, no_event_stream: None
) -> AsyncIterator[tuple[MockConfigEntry, FakeLiveNvr]]:
    """An NVR entry whose relay streams from a fake NVR."""
    fake = FakeLiveNvr()
    server = await asyncio.start_server(fake.serve, "127.0.0.1", 0)
    client = make_nvr_client("127.0.0.1", server.sockets[0].getsockname()[1])
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=NVR_MAC, data={**NVR_DATA, CONF_HOST: "127.0.0.1"}
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ajax_doorbell.hub.AjaxOnvifClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    yield entry, fake
    assert await hass.config_entries.async_unload(entry.entry_id)
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


async def test_live_relay(live: tuple[MockConfigEntry, FakeLiveNvr]) -> None:
    """go2rtc gets the camera through the relay: tracks received only, NVR account added."""
    entry, fake = live
    hub = entry.runtime_data
    local = f"rtsp://127.0.0.1:{hub.port}/live/{TURRET}/main"
    reader, writer = await asyncio.open_connection("127.0.0.1", hub.port)
    try:
        head, body = await _request(
            reader,
            writer,
            f"DESCRIBE {local} RTSP/1.0\r\nCSeq: 1\r\n"
            "Require: www.onvif.org/ver20/backchannel\r\n\r\n",
        )
        assert head.startswith("RTSP/1.0 200")
        text = body.decode()
        assert text.count("a=recvonly") == 2
        assert "sendrecv" not in text
        assert f"a=control:{local}/trackID=1" in text
        assert f"Content-Base: {local}/" in head
        # go2rtc's backchannel requirement is not passed on: the NVR gets a plain viewer
        describe = next(r for r in fake.requests if r.startswith("DESCRIBE") and "Digest" in r)
        assert "Require" not in describe
        assert describe.startswith("DESCRIBE rtsp://127.0.0.1:")
        head, _ = await _request(
            reader,
            writer,
            f"SETUP {local}/trackID=1 RTSP/1.0\r\nCSeq: 2\r\nTransport: RTP/AVP;unicast\r\n\r\n",
        )
        assert head.startswith("RTSP/1.0 461")
        head, _ = await _request(
            reader,
            writer,
            f"SETUP {local}/trackID=1 RTSP/1.0\r\nCSeq: 3\r\n"
            "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n",
        )
        assert head.startswith("RTSP/1.0 200")
        head, _ = await _request(
            reader, writer, f"PLAY {local} RTSP/1.0\r\nCSeq: 4\r\nSession: 5\r\n\r\n"
        )
        assert head.startswith("RTSP/1.0 200")
        frame = await asyncio.wait_for(reader.readexactly(4 + 16), 2)
        assert frame.startswith(b"$\x00") and frame.endswith(b"live")
    finally:
        writer.close()


async def test_live_relay_unknown_camera(live: tuple[MockConfigEntry, FakeLiveNvr]) -> None:
    """A path for no camera of this NVR is a 404."""
    entry, _ = live
    port = entry.runtime_data.port
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        head, _ = await _request(
            reader,
            writer,
            f"DESCRIBE rtsp://127.0.0.1:{port}/live/nobody/main RTSP/1.0\r\nCSeq: 1\r\n\r\n",
        )
        assert head.startswith("RTSP/1.0 404")
    finally:
        writer.close()


# ------------------------------------------------------------- replay probe


class FakeReplay:
    """Replays from a moment, stamping packets with the ONVIF NTP extension."""

    def __init__(self, jump: float | None) -> None:
        self.jump = jump  # seconds after the asked moment; None: no video at all

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request = (await reader.readuntil(b"\r\n\r\n")).decode()
                method, uri = request.split(" ", 2)[:2]
                cseq = re.search(r"CSeq: (\d+)", request).group(1)
                if method in ("DESCRIBE", "SETUP", "PLAY"):
                    assert "Require: onvif-replay" in request
                headers, body = "", ""
                if method == "DESCRIBE":
                    body = "v=0\r\nm=video 0 RTP/AVP 96\r\na=control:streamid=0\r\n"
                    headers = f"Content-Base: {uri}/\r\nContent-Length: {len(body)}\r\n"
                elif method == "SETUP":
                    headers = "Session: 9\r\nTransport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"
                writer.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{headers}\r\n{body}".encode())
                if method == "PLAY" and self.jump is not None:
                    clock = re.search(r"clock=(\d{8}T\d{6})Z", request).group(1)
                    asked = datetime.strptime(clock, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
                    seconds = (asked - NTP_EPOCH).total_seconds() + self.jump
                    extension = struct.pack(
                        ">HHII", 0xABAC, 3, int(seconds), int((seconds % 1) * 2**32)
                    ) + bytes(4)
                    rtp = bytes([0x90, 96, 0, 1]) + bytes(8) + extension + b"frame"
                    writer.write(b"$\x00" + len(rtp).to_bytes(2, "big") + rtp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.mark.parametrize(("jump", "expected"), [(0.5, 0.5), (1490.0, 1490.0), (None, None)])
async def test_probe_replay(
    socket_enabled: None, jump: float | None, expected: float | None
) -> None:
    """The first replayed frame tells when the video really starts."""
    server = await asyncio.start_server(FakeReplay(jump).serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    asked = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)
    try:
        result = await probe_replay(
            f"rtsp://127.0.0.1:{port}/{TURRET}_rm", asked, "homeassistant", "secret", wait=0.5
        )
    finally:
        server.close()
        await server.wait_closed()
    assert result["delta_s"] == expected
    assert "error" not in result


# ------------------------------------------------------------- config flow

DHCP = DhcpServiceInfo(ip=NVR_HOST, hostname="nvr", macaddress=NVR_MAC.replace(":", ""))


@pytest.fixture
def no_setup() -> Generator[None]:
    """Create entries without setting them up."""
    with patch("custom_components.ajax_doorbell.async_setup_entry", return_value=True):
        yield


@pytest.mark.usefixtures("no_setup")
async def test_dhcp_nvr_reuses_the_doorbell_account(
    hass: HomeAssistant, mock_client: MagicMock, mock_entry: MockConfigEntry
) -> None:
    """The NVR found on the network takes the account saved in the DoorBell's options."""
    mock_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_entry, options={"nvr_host": NVR_HOST, "nvr_username": "", "nvr_password": ""}
    )
    mock_client.async_get_services.return_value = {"device": "d", "search": "s", "replay": "r"}
    mock_client.async_get_mac.return_value = NVR_MAC
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
    )
    assert result["step_id"] == "nvr"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "homeassistant"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_HOST: NVR_HOST,
        CONF_PORT: 8080,
        CONF_USERNAME: "homeassistant",
        CONF_PASSWORD: "secret",  # the DoorBell's own, which its NVR options fall back to
        "kind": "nvr",
    }
    assert result["result"].unique_id == NVR_MAC


@pytest.mark.usefixtures("no_setup")
async def test_dhcp_nvr_needs_a_password(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Without a DoorBell that knows the NVR, its password must be typed."""
    mock_client.async_get_services.return_value = {"device": "d", "search": "s", "replay": "r"}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "homeassistant"}
    )
    assert result["errors"] == {"base": "password_required"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "homeassistant", CONF_PASSWORD: "nvr-secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] == "nvr-secret"


@pytest.mark.usefixtures("no_setup")
async def test_nvr_reauth(
    hass: HomeAssistant, mock_client: MagicMock, nvr_entry: MockConfigEntry
) -> None:
    """A refused NVR account is typed again; the check is the NVR's, not a DoorBell's."""
    nvr_entry.add_to_hass(hass)
    mock_client.async_get_services.return_value = {"device": "d", "search": "s", "replay": "r"}
    mock_client.async_get_mac.return_value = NVR_MAC
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": nvr_entry.entry_id},
        data=nvr_entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "homeassistant", CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert nvr_entry.data[CONF_PASSWORD] == "new"
    assert nvr_entry.data["kind"] == "nvr"
