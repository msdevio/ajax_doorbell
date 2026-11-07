"""Ajax DoorBell: a local intercom for Home Assistant.

Live video and listening go through Home Assistant's go2rtc over the doorbell's
own RTSP stream; talk-back goes from the card to the doorbell's audio
backchannel through this integration; rings arrive as local ONVIF events.
Nothing goes through the Ajax cloud.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components import websocket_api
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .doorbell import AjaxDoorbell
from .onvif import OnvifAuthError, OnvifError
from .services import async_setup_services
from .talk import websocket_talk
from .visits_api import VisitThumbnailView, websocket_replay, websocket_visits

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CAMERA, Platform.EVENT, Platform.SENSOR]
CARD_FILE = Path(__file__).parent / "frontend" / "ajax-doorbell-card.js"
CARD_URL = f"/{DOMAIN}/ajax-doorbell-card.js"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AjaxDoorbellConfigEntry = ConfigEntry[AjaxDoorbell]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services, the talk command and the intercom card once."""
    async_setup_services(hass)
    websocket_api.async_register_command(hass, websocket_talk)
    websocket_api.async_register_command(hass, websocket_visits)
    websocket_api.async_register_command(hass, websocket_replay)
    if hass.http is not None:
        hass.http.register_view(VisitThumbnailView())
    await _async_register_card(hass)
    return True


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the intercom card and load it on every dashboard: nothing to copy by hand.

    The URL carries a hash of the file, so browsers fetch each new version at once.
    """
    if hass.http is None or "frontend" not in hass.config.components:
        return  # no web server (tests, headless): nothing to serve
    content = await hass.async_add_executor_job(CARD_FILE.read_bytes)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=True)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={hashlib.sha256(content).hexdigest()[:10]}")


async def async_setup_entry(hass: HomeAssistant, entry: AjaxDoorbellConfigEntry) -> bool:
    """Connect to the DoorBell and start listening to it."""
    doorbell = AjaxDoorbell(hass, entry)
    try:
        await doorbell.async_initialize()
    except OnvifAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except OnvifError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err
    entry.runtime_data = doorbell
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    doorbell.start(entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AjaxDoorbellConfigEntry) -> bool:
    """Stop listening and remove the entities."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
    return unloaded
