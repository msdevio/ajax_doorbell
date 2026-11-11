"""What the card uses to list visits and replay them from the NVR."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from aiohttp import web
import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.http import KEY_HASS
from homeassistant.util import dt as dt_util

from .const import DOMAIN, is_nvr_entry
from .nvr import NvrError
from .talk import _doorbell_for

if TYPE_CHECKING:
    from .doorbell import AjaxDoorbell

THUMB_URL = f"/api/{DOMAIN}/thumb/{{entry_id}}/{{visit_id}}"
# The image a little after the visit started: the visitor is in view.
THUMB_DELAY = timedelta(seconds=1)
SIGNED_FOR = timedelta(hours=6)


def _replay_camera(hass: HomeAssistant, doorbell: AjaxDoorbell) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "camera", DOMAIN, f"{doorbell.unique_base}_replay"
    )


def _payload(
    hass: HomeAssistant, doorbell: AjaxDoorbell, connection: websocket_api.ActiveConnection
) -> dict[str, Any]:
    nvr = doorbell.nvr is not None
    visits = []
    for visit in reversed(doorbell.visits.visits):
        thumb = None
        if nvr:
            path = THUMB_URL.format(entry_id=doorbell.entry.entry_id, visit_id=visit.id)
            thumb = async_sign_path(
                hass, path, SIGNED_FOR, refresh_token_id=connection.refresh_token_id
            )
        visits.append(
            {
                "id": visit.id,
                "start": visit.start.isoformat(),
                "end": visit.end.isoformat(),
                "kind": visit.kind,
                "thumb": thumb,
            }
        )
    start = doorbell.replay_start
    return {
        "visits": visits,
        "nvr": nvr,
        "nvr_configured": doorbell.nvr_configured,
        "nvr_error": doorbell.nvr_error,
        "replay_camera": _replay_camera(hass, doorbell),
        "replay_start": start.isoformat() if start else None,
    }


def _check(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> AjaxDoorbell | None:
    entity_id = msg["entity_id"]
    if not connection.user.permissions.check_entity(entity_id, POLICY_READ):
        raise Unauthorized(
            context=connection.context(msg), entity_id=entity_id, permission=POLICY_READ
        )
    if (doorbell := _doorbell_for(hass, entity_id)) is None:
        connection.send_error(msg["id"], "not_found", f"{entity_id} is not an Ajax DoorBell")
    return doorbell


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/visits", vol.Required("entity_id"): cv.entity_id}
)
@callback
def websocket_visits(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send the visits of the last days, then again on every change."""
    if (doorbell := _check(hass, connection, msg)) is None:
        return

    @callback
    def send() -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], _payload(hass, doorbell, connection))
        )

    unsubscribes = [
        doorbell.visits.async_add_listener(send),
        doorbell.async_add_replay_listener(send),
    ]

    @callback
    def unsubscribe() -> None:
        for remove in unsubscribes:
            remove()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    send()


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/replay",
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("start"): str,
    }
)
@callback
def websocket_replay(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Point the replay camera at a moment; the card then opens it like a live camera."""
    if (doorbell := _check(hass, connection, msg)) is None:
        return
    start = dt_util.parse_datetime(msg["start"])
    if start is None or start.tzinfo is None:
        connection.send_error(msg["id"], "invalid_format", "start must be an ISO time with a zone")
        return
    try:
        doorbell.set_replay(min(start, dt_util.utcnow()))
    except NvrError as err:
        connection.send_error(msg["id"], "nvr_unavailable", str(err))
        return
    connection.send_result(msg["id"], {"camera": _replay_camera(hass, doorbell)})


class VisitThumbnailView(HomeAssistantView):
    """A visit's image, taken from the NVR when asked and kept in memory only."""

    url = THUMB_URL
    name = f"api:{DOMAIN}:thumb"

    async def get(self, request: web.Request, entry_id: str, visit_id: str) -> web.Response:
        """Return the JPEG, or 404."""
        hass = request.app[KEY_HASS]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise web.HTTPNotFound
        if is_nvr_entry(entry):
            raise web.HTTPNotFound
        doorbell: AjaxDoorbell = entry.runtime_data
        visit = doorbell.visits.get(visit_id)
        if visit is None or doorbell.nvr is None:
            raise web.HTTPNotFound
        image = await doorbell.nvr.async_thumbnail(visit.start + THUMB_DELAY)
        if image is None:
            raise web.HTTPNotFound
        return web.Response(
            body=image,
            content_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )
