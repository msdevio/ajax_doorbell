"""Call services: answer, hang up, dismiss, clear missed calls."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_config_entry_ids

from .const import DOMAIN, is_nvr_entry
from .doorbell import AjaxDoorbell

ATTR_ANSWERED_BY = "answered_by"

SERVICE_ANSWER = "answer"
SERVICE_HANG_UP = "hang_up"
SERVICE_DISMISS = "dismiss"
SERVICE_CLEAR_MISSED = "clear_missed"

TARGET_SCHEMA = vol.Schema(cv.TARGET_SERVICE_FIELDS)
ANSWER_SCHEMA = TARGET_SCHEMA.extend({vol.Optional(ATTR_ANSWERED_BY): cv.string})


async def _async_doorbells(call: ServiceCall) -> list[AjaxDoorbell]:
    """Resolve the target; no target means every DoorBell."""
    entries = [
        entry
        for entry in call.hass.config_entries.async_loaded_entries(DOMAIN)
        if not is_nvr_entry(entry)  # an NVR has no calls
    ]
    if any(call.data.get(key) for key in cv.TARGET_SERVICE_FIELDS):
        wanted = await async_extract_config_entry_ids(call)
        entries = [entry for entry in entries if entry.entry_id in wanted]
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_doorbell")
    return [entry.runtime_data for entry in entries]


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the call services."""

    async def answer(call: ServiceCall) -> None:
        for doorbell in await _async_doorbells(call):
            doorbell.call.answer(call.data.get(ATTR_ANSWERED_BY))

    async def hang_up(call: ServiceCall) -> None:
        for doorbell in await _async_doorbells(call):
            doorbell.call.hang_up()

    async def dismiss(call: ServiceCall) -> None:
        for doorbell in await _async_doorbells(call):
            doorbell.call.dismiss()

    async def clear_missed(call: ServiceCall) -> None:
        for doorbell in await _async_doorbells(call):
            doorbell.call.clear_missed()

    hass.services.async_register(DOMAIN, SERVICE_ANSWER, answer, schema=ANSWER_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_HANG_UP, hang_up, schema=TARGET_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DISMISS, dismiss, schema=TARGET_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_MISSED, clear_missed, schema=TARGET_SCHEMA)
