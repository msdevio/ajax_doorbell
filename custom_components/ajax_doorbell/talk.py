"""Talk to the visitor: the browser's voice on the DoorBell's audio backchannel.

The DoorBell only takes G.722 on its backchannel, which go2rtc cannot produce
from a browser. So the card encodes the microphone itself and sends 20 ms
frames as binary messages on the Home Assistant websocket; this module wraps
them in RTP on an RTSP session of its own, next to go2rtc's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import logging
import re
import secrets
import struct
import time
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import DOMAIN
from .rtsp import (
    REQUIRE_BACKCHANNEL,
    RtspConnection,
    RtspError,
    StreamDescription,
    media_url,
    parse_sdp,
)

if TYPE_CHECKING:
    from .doorbell import AjaxDoorbell

_LOGGER = logging.getLogger(__name__)

# Codecs the card encodes, with their sample rate: each is 160 bytes per 20 ms.
TALK_ENCODINGS = {"G722": 16000, "PCMA": 8000, "PCMU": 8000}
FRAME_BYTES = 160
FRAME_SECONDS = 0.02
# More unsent audio than this (one second) and frames are dropped: the link stalls.
MAX_BACKLOG = 8000
# A pause longer than this starts a new talk spurt (marker bit, timestamp jump).
SPURT_GAP = 0.2


class TalkSession:
    """One backchannel session: frames in, RTP out to the DoorBell speaker."""

    def __init__(self, uri: str, username: str, password: str, owner: str) -> None:
        """Prepare the session for one talker; nothing is opened yet."""
        self.owner = owner
        self._connection = RtspConnection(uri, username, password)
        self._base = self._connection.uri
        self.codec = ""
        self.sample_rate = 0
        self._clock = 8000
        self._payload_type = 0
        self._channel = 0
        self._ssrc = secrets.randbits(32)
        self._sequence = secrets.randbits(16)
        self._timestamp = secrets.randbits(32)
        self._last_frame: float | None = None
        self._keepalive: asyncio.Task[None] | None = None
        self._end_listeners: list[Callable[[str], None]] = []
        self.ended: str | None = None
        self.sent = 0
        self.dropped = 0

    async def async_open(self) -> None:
        """DESCRIBE, SETUP the backchannel track alone, PLAY."""
        connection = self._connection
        require = {"Require": REQUIRE_BACKCHANNEL}
        await connection.connect()
        try:
            status, headers, body = await connection.request(
                "DESCRIBE", headers={"Accept": "application/sdp", **require}
            )
            if status != 200:
                raise RtspError(f"{connection.host}: DESCRIBE refused ({status})")
            self._base = headers.get("content-base", connection.uri)
            medias = parse_sdp(body.decode(errors="replace"))
            track = StreamDescription(status, medias).backchannel
            if track is None:
                raise RtspError(f"{connection.host}: no audio backchannel")
            for codec, payload_type in zip(track.codecs, track.payload_types, strict=False):
                name, _, rate = codec.partition("/")
                if name.upper() in TALK_ENCODINGS:
                    self.codec = name.upper()
                    self.sample_rate = TALK_ENCODINGS[self.codec]
                    self._clock = int(rate.split("/")[0] or self.sample_rate)
                    self._payload_type = payload_type
                    break
            else:
                raise RtspError(f"{connection.host}: backchannel codec {track.codecs} unsupported")
            status, headers, _ = await connection.request(
                "SETUP",
                media_url(self._base, track.control),
                {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1", **require},
            )
            if status != 200:
                raise RtspError(f"{connection.host}: backchannel SETUP refused ({status})")
            if found := re.search(r"interleaved=(\d+)", headers.get("transport", "")):
                self._channel = int(found.group(1))
            status, _, _ = await connection.request(
                "PLAY", self._base, {"Range": "npt=0.000-", **require}
            )
            if status != 200:
                raise RtspError(f"{connection.host}: backchannel PLAY refused ({status})")
        except BaseException:
            await connection.close()
            raise
        connection.start_pump()
        self._keepalive = asyncio.get_running_loop().create_task(self._keep_alive())
        _LOGGER.debug(
            "%s: talking as %s, %s PT %s clock %s channel %s",
            connection.host,
            self.owner,
            self.codec,
            self._payload_type,
            self._clock,
            self._channel,
        )

    async def _keep_alive(self) -> None:
        """Refresh the RTSP session; end the talk if the device drops it."""
        interval = max(5, min(30, self._connection.session_timeout / 2))
        try:
            while True:
                with suppress(TimeoutError):
                    async with asyncio.timeout(interval):
                        await self._connection.closed.wait()
                    self._end("closed")
                    return
                await self._connection.request("OPTIONS", self._base)
        except RtspError as err:
            _LOGGER.debug("%s: talk session lost: %s", self._connection.host, err)
            self._end("closed")

    @callback
    def feed(self, payload: bytes) -> None:
        """Send audio from the browser: whole 20 ms frames of the session codec."""
        if self.ended is not None:
            return
        now = time.monotonic()
        marker = self._last_frame is None
        if self._last_frame is not None and now - self._last_frame > SPURT_GAP:
            # After a pause (push to talk), timestamps follow the wall clock again.
            gap = now - self._last_frame - FRAME_SECONDS
            self._timestamp += round(gap * self._clock)
            marker = True
        self._last_frame = now
        step = self._clock // 50
        for start in range(0, len(payload) - FRAME_BYTES + 1, FRAME_BYTES):
            if self._connection.backlog > MAX_BACKLOG:
                self.dropped += 1
            else:
                header = struct.pack(
                    ">BBHII",
                    0x80,
                    self._payload_type | (0x80 if marker else 0),
                    self._sequence & 0xFFFF,
                    self._timestamp & 0xFFFFFFFF,
                    self._ssrc,
                )
                self._connection.send_interleaved(
                    self._channel, header + payload[start : start + FRAME_BYTES]
                )
                self.sent += 1
                marker = False
            self._sequence += 1
            self._timestamp += step

    @callback
    def add_end_listener(self, listener: Callable[[str], None]) -> None:
        """Call listener with the reason once the session ends."""
        self._end_listeners.append(listener)

    @callback
    def _end(self, reason: str) -> None:
        if self.ended is not None:
            return
        self.ended = reason
        for listener in list(self._end_listeners):
            listener(reason)

    async def async_close(self, reason: str = "closed") -> None:
        """End the session: TEARDOWN, then close the connection."""
        self._end(reason)
        if (task := self._keepalive) is not None:
            self._keepalive = None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._connection.close(self._base)

    def as_dict(self) -> dict[str, Any]:
        """Return what diagnostics show of the session."""
        return {
            "owner": self.owner,
            "codec": self.codec,
            "clock": self._clock,
            "frames_sent": self.sent,
            "frames_dropped": self.dropped,
            "ended": self.ended,
        }


def _doorbell_for(hass: HomeAssistant, entity_id: str) -> AjaxDoorbell | None:
    """Return the DoorBell behind one of its entities."""
    entry_id = (registry := er.async_get(hass).async_get(entity_id)) and registry.config_entry_id
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
        return None
    return entry.runtime_data


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/talk",
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("name"): str,
    }
)
@websocket_api.async_response
async def websocket_talk(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Open the DoorBell speaker to this browser until it unsubscribes.

    Events: {"type": "ready", "handler_id", "codec", "sample_rate", "frame_bytes"}
    once binary frames ([handler_id] + audio) are welcome, then
    {"type": "ended", "reason"} if the talk ends from this side.
    """
    entity_id = msg["entity_id"]
    if not connection.user.permissions.check_entity(entity_id, POLICY_CONTROL):
        raise Unauthorized(
            context=connection.context(msg), entity_id=entity_id, permission=POLICY_CONTROL
        )
    if (doorbell := _doorbell_for(hass, entity_id)) is None:
        connection.send_error(msg["id"], "not_found", f"{entity_id} is not an Ajax DoorBell")
        return
    owner = msg.get("name") or connection.user.name or ""
    try:
        session = await doorbell.async_start_talk(owner)
    except RtspError as err:
        connection.send_error(msg["id"], "talk_failed", str(err))
        return

    @callback
    def frame(
        _hass: HomeAssistant, _connection: websocket_api.ActiveConnection, data: bytes
    ) -> None:
        session.feed(data)

    handler_id, unregister = connection.async_register_binary_handler(frame)

    @callback
    def ended(reason: str) -> None:
        unregister()
        connection.send_message(
            websocket_api.event_message(msg["id"], {"type": "ended", "reason": reason})
        )

    @callback
    def unsubscribe() -> None:
        unregister()
        hass.async_create_task(doorbell.async_stop_talk(session))

    session.add_end_listener(ended)
    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    connection.send_message(
        websocket_api.event_message(
            msg["id"],
            {
                "type": "ready",
                "handler_id": handler_id,
                "codec": session.codec,
                "sample_rate": session.sample_rate,
                "frame_bytes": FRAME_BYTES,
            },
        )
    )
