"""Listen to the visitor: the DoorBell's microphone, relayed to the card.

Home Assistant's go2rtc never delivers the DoorBell's G.722 audio to the
browser (its Opus transcoding step stays silent), so the integration reads
the audio track itself and passes the frames to the card, which decodes and
plays them. Live listening is one RTSP session shared by every screen; a
replay from the NVR gets a session of its own.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from contextlib import suppress
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .nvr import NvrError
from .rtsp import RtspConnection, RtspError, StreamDescription, media_url, parse_sdp, rtp_payload
from .talk import _doorbell_for

_LOGGER = logging.getLogger(__name__)

# Codecs the card decodes, with their sample rate.
LISTEN_ENCODINGS = {"G722": 16000, "PCMA": 8000, "PCMU": 8000}


class ListenSession:
    """One RTSP session on a stream's audio track, its frames handed to listeners."""

    def __init__(self, uri: str, username: str, password: str) -> None:
        """Prepare the session; nothing is opened yet."""
        self._connection = RtspConnection(uri, username, password)
        self._connection.on_frame = self._frame
        self._base = self._connection.uri
        self.codec = ""
        self.sample_rate = 0
        self._channel = 0
        self._listeners: list[Callable[[bytes], None]] = []
        self._end_listeners: list[Callable[[str], None]] = []
        self._keepalive: asyncio.Task[None] | None = None
        self.ended: str | None = None
        self.frames = 0

    async def async_open(self) -> None:
        """DESCRIBE, SETUP the audio track alone, PLAY."""
        connection = self._connection
        await connection.connect()
        try:
            status, headers, body = await connection.request(
                "DESCRIBE", headers={"Accept": "application/sdp"}
            )
            if status != 200:
                raise RtspError(f"{connection.host}: DESCRIBE refused ({status})")
            self._base = headers.get("content-base", connection.uri)
            track = StreamDescription(status, parse_sdp(body.decode(errors="replace"))).audio
            if track is None or not track.codecs:
                raise RtspError(f"{connection.host}: no audio track")
            name = track.codecs[0].split("/", 1)[0].upper()
            if name not in LISTEN_ENCODINGS:
                raise RtspError(f"{connection.host}: audio codec {track.codecs[0]} unsupported")
            self.codec, self.sample_rate = name, LISTEN_ENCODINGS[name]
            status, headers, _ = await connection.request(
                "SETUP",
                media_url(self._base, track.control),
                {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
            )
            if status != 200:
                raise RtspError(f"{connection.host}: audio SETUP refused ({status})")
            if found := re.search(r"interleaved=(\d+)", headers.get("transport", "")):
                self._channel = int(found.group(1))
            status, _, _ = await connection.request("PLAY", self._base, {"Range": "npt=0.000-"})
            if status != 200:
                raise RtspError(f"{connection.host}: PLAY refused ({status})")
        except BaseException:
            await connection.close()
            raise
        connection.start_pump()
        self._keepalive = asyncio.get_running_loop().create_task(self._keep_alive())

    async def _keep_alive(self) -> None:
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
            _LOGGER.debug("Listening session lost: %s", err)
            self._end("closed")

    def _frame(self, channel: int, packet: bytes) -> None:
        if channel != self._channel or (payload := rtp_payload(packet)) is None:
            return
        self.frames += 1
        for listener in list(self._listeners):
            listener(payload)

    def add_listener(self, listener: Callable[[bytes], None]) -> Callable[[], None]:
        """Hand audio frames to listener; returns how to stop."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    @property
    def listeners(self) -> int:
        """Return how many listen."""
        return len(self._listeners)

    def add_end_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """Call listener with the reason once the session ends; returns how to stop."""
        self._end_listeners.append(listener)
        return lambda: (
            self._end_listeners.remove(listener) if listener in self._end_listeners else None
        )

    def _end(self, reason: str) -> None:
        if self.ended is not None:
            return
        self.ended = reason
        for listener in list(self._end_listeners):
            listener(reason)

    async def async_close(self, reason: str = "closed") -> None:
        """End the session."""
        self._end(reason)
        if (task := self._keepalive) is not None:
            self._keepalive = None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._connection.close(self._base)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/listen",
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("start"): str,
    }
)
@websocket_api.async_response
async def websocket_listen(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send the DoorBell's audio frames (base64) until the browser unsubscribes.

    Events: {"type": "ready", "codec", "sample_rate"}, then {"a": frame} every
    few tens of milliseconds, and {"type": "ended", "reason"} if it stops.
    With "start", the audio is the NVR's recording from that moment.
    """
    entity_id = msg["entity_id"]
    if not connection.user.permissions.check_entity(entity_id, POLICY_READ):
        raise Unauthorized(
            context=connection.context(msg), entity_id=entity_id, permission=POLICY_READ
        )
    if (doorbell := _doorbell_for(hass, entity_id)) is None:
        connection.send_error(msg["id"], "not_found", f"{entity_id} is not an Ajax DoorBell")
        return
    start = None
    if "start" in msg:
        start = dt_util.parse_datetime(msg["start"])
        if start is None or start.tzinfo is None:
            connection.send_error(
                msg["id"], "invalid_format", "start must be an ISO time with a zone"
            )
            return

    @callback
    def frame(payload: bytes) -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], {"a": base64.b64encode(payload).decode()})
        )

    try:
        session, stop = await doorbell.async_listen(frame, start)
    except (RtspError, NvrError) as err:
        connection.send_error(msg["id"], "listen_failed", str(err))
        return

    @callback
    def ended(reason: str) -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], {"type": "ended", "reason": reason})
        )

    forget_end = session.add_end_listener(ended)

    @callback
    def unsubscribe() -> None:
        forget_end()
        stop()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    connection.send_message(
        websocket_api.event_message(
            msg["id"], {"type": "ready", "codec": session.codec, "sample_rate": session.sample_rate}
        )
    )
