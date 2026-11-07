"""Ajax DoorBell: a local intercom for Home Assistant.

Live video, listening and talk-back go through Home Assistant's go2rtc over
the doorbell's own RTSP stream; rings arrive as local ONVIF events. Nothing
goes through the Ajax cloud.
"""

from __future__ import annotations

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

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CAMERA, Platform.EVENT, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AjaxDoorbellConfigEntry = ConfigEntry[AjaxDoorbell]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services once."""
    async_setup_services(hass)
    return True


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
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: AjaxDoorbellConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AjaxDoorbellConfigEntry) -> bool:
    """Stop listening and remove the entities."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
    return unloaded
