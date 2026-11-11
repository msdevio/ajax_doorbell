"""What the camera card uses: a camera's detections, recorded spans and replays."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

from aiohttp import web
import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.http import KEY_HASS
from homeassistant.util import dt as dt_util

from .const import DOMAIN, is_nvr_entry
from .hub import STAMP
from .onvif import OnvifError

if TYPE_CHECKING:
    from .hub import AjaxNvrHub, Channel

THUMB_URL = f"/api/{DOMAIN}/nvr_thumb/{{entry_id}}/{{group}}/{{stamp}}"
# The picture a second after the detection began: whoever moved is in view.
THUMB_DELAY = timedelta(seconds=1)
SIGNED_FOR = timedelta(hours=6)
# Every detection is a mark on the timeline; the newest ones also get a picture.
WITH_PICTURES = 30
MAX_SPAN = timedelta(days=31)
# Activity can update a camera's journal every second: send at most this often.
SEND_DELAY = 1.0


def camera_for(hass: HomeAssistant, entity_id: str) -> tuple[AjaxNvrHub, Channel] | None:
    """Return the NVR and the camera behind one of its cameras' entities."""
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None or entity.platform != DOMAIN or entity.config_entry_id is None:
        return None
    entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    if entry is None or not is_nvr_entry(entry) or entry.state is not ConfigEntryState.LOADED:
        return None
    hub: AjaxNvrHub = entry.runtime_data
    for channel in hub.channels.values():
        if entity.unique_id.startswith(f"{hub.unique_base}_{channel.group}_"):
            return hub, channel
    return None


def _check(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> tuple[AjaxNvrHub, Channel] | None:
    entity_id = msg["entity_id"]
    if not connection.user.permissions.check_entity(entity_id, POLICY_READ):
        raise Unauthorized(
            context=connection.context(msg), entity_id=entity_id, permission=POLICY_READ
        )
    if (found := camera_for(hass, entity_id)) is None:
        connection.send_error(msg["id"], "not_found", f"{entity_id} is not a camera of an Ajax NVR")
    return found


def _time(value: str) -> datetime | None:
    moment = dt_util.parse_datetime(value)
    return moment if moment is not None and moment.tzinfo is not None else None


def _payload(
    hass: HomeAssistant,
    hub: AjaxNvrHub,
    channel: Channel,
    connection: websocket_api.ActiveConnection,
) -> dict[str, Any]:
    replay = er.async_get(hass).async_get_entity_id(
        "camera", DOMAIN, f"{hub.unique_base}_{channel.group}_replay"
    )
    detections = []
    for index, detection in enumerate(reversed(hub.journal.cameras.get(channel.group, []))):
        thumb = None
        if channel.recordings and index < WITH_PICTURES:
            stamp = (detection.start + THUMB_DELAY).astimezone(UTC).strftime(STAMP)
            path = THUMB_URL.format(
                entry_id=hub.entry.entry_id, group=quote(channel.group, safe=""), stamp=stamp
            )
            thumb = async_sign_path(
                hass, path, SIGNED_FOR, refresh_token_id=connection.refresh_token_id
            )
        detections.append(
            {
                "id": detection.id,
                "start": detection.start.isoformat(),
                "end": detection.end.isoformat(),
                "kinds": sorted(detection.kinds),
                "thumb": thumb,
            }
        )
    recording = channel.recording_main
    return {
        "name": channel.name,
        "mode": channel.mode,
        "recording": channel.recording,
        "recorded": bool(channel.recordings),
        "earliest": recording.earliest.isoformat() if recording and recording.earliest else None,
        "replay_camera": replay,
        "replay_start": channel.replay_start.isoformat() if channel.replay_start else None,
        "retention_days": hub.retention_days,
        "detections": detections,
    }


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/nvr_camera", vol.Required("entity_id"): cv.entity_id}
)
@callback
def websocket_nvr_camera(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send a camera's state and detections, then again whenever they change."""
    if (found := _check(hass, connection, msg)) is None:
        return
    hub, channel = found
    pending: list[CALLBACK_TYPE] = []

    @callback
    def send(_now: datetime | None = None) -> None:
        pending.clear()
        connection.send_message(
            websocket_api.event_message(msg["id"], _payload(hass, hub, channel, connection))
        )

    @callback
    def soon(group: str | None = None) -> None:
        if (group is None or group == channel.group) and not pending:
            pending.append(async_call_later(hass, SEND_DELAY, send))

    unsubscribes: list[Callable[[], None]] = [
        hub.journal.async_add_listener(soon),
        channel.async_add_listener(soon),
        channel.async_add_replay_listener(soon),
    ]

    @callback
    def unsubscribe() -> None:
        for remove in unsubscribes + pending:
            remove()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    send()


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/nvr_segments",
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("start"): str,
        vol.Required("end"): str,
    }
)
@websocket_api.async_response
async def websocket_nvr_segments(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the spans of a time range the NVR holds video for."""
    if (found := _check(hass, connection, msg)) is None:
        return
    hub, channel = found
    start, end = _time(msg["start"]), _time(msg["end"])
    if start is None or end is None or not timedelta(0) < end - start <= MAX_SPAN:
        connection.send_error(msg["id"], "invalid_format", "start and end: ISO times, end later")
        return
    try:
        segments = await hub.async_segments(channel, start, end)
    except OnvifError as err:
        connection.send_error(msg["id"], "nvr_unavailable", str(err))
        return
    connection.send_result(
        msg["id"], {"segments": [[s.isoformat(), e.isoformat()] for s, e in segments]}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/nvr_replay",
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("start"): str,
    }
)
@callback
def websocket_nvr_replay(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Point a camera's replay at a moment; the card then plays the replay camera."""
    if (found := _check(hass, connection, msg)) is None:
        return
    hub, channel = found
    if (start := _time(msg["start"])) is None:
        connection.send_error(msg["id"], "invalid_format", "start must be an ISO time with a zone")
        return
    if not channel.recordings:
        connection.send_error(msg["id"], "not_recorded", f"{channel.name} has no recording")
        return
    start = min(start, dt_util.utcnow())
    hub.set_replay(channel, start)
    replay = er.async_get(hass).async_get_entity_id(
        "camera", DOMAIN, f"{hub.unique_base}_{channel.group}_replay"
    )
    connection.send_result(msg["id"], {"camera": replay, "start": start.isoformat()})


class NvrThumbnailView(HomeAssistantView):
    """A camera's picture at a moment, taken from the NVR and kept in memory only."""

    url = THUMB_URL
    name = f"api:{DOMAIN}:nvr_thumb"

    async def get(
        self, request: web.Request, entry_id: str, group: str, stamp: str
    ) -> web.Response:
        """Return the JPEG, or 404."""
        hass = request.app[KEY_HASS]
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or not is_nvr_entry(entry)
            or entry.state is not ConfigEntryState.LOADED
        ):
            raise web.HTTPNotFound
        hub: AjaxNvrHub = entry.runtime_data
        channel = hub.channels.get(unquote(group))
        try:
            moment = datetime.strptime(stamp, STAMP).replace(tzinfo=UTC)
        except ValueError:
            raise web.HTTPNotFound from None
        if channel is None or (image := await hub.async_thumbnail(channel, moment)) is None:
            raise web.HTTPNotFound
        return web.Response(
            body=image,
            content_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )
