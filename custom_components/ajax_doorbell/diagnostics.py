"""Diagnostics: what the DoorBell exposes, with secrets removed."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import AjaxDoorbellConfigEntry
from .const import is_nvr_entry
from .rtsp import strip_credentials

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    "nvr_password",
    "nvr_username",
    "serial",
    "mac",
    "unique_id",
}


def _hide_mac(uri: str, mac: str | None) -> str:
    """Ajax builds stream paths from the MAC address."""
    if not mac:
        return uri
    return uri.replace(mac.replace(":", "").lower(), "**REDACTED**")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AjaxDoorbellConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    if is_nvr_entry(entry):
        return {
            "entry": async_redact_data({"data": dict(entry.data)}, TO_REDACT),
            "nvr": entry.runtime_data.as_diagnostics(),
        }
    doorbell = entry.runtime_data
    call = doorbell.call
    description = doorbell.description
    return {
        "entry": async_redact_data(
            {"data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT
        ),
        "device": async_redact_data(
            {
                **(asdict(doorbell.identity) if doorbell.identity else {}),
                "mac": doorbell.mac,
                "http_digest": doorbell.client.uses_http_digest,
                "clock_offset_s": round(doorbell.client.time_offset.total_seconds(), 1),
                "services": doorbell.client.services,
            },
            TO_REDACT,
        ),
        "profiles": [asdict(profile) for profile in doorbell.profiles],
        "stream_uris": {
            token: _hide_mac(strip_credentials(uri), doorbell.mac)
            for token, uri in doorbell.stream_uris.items()
        },
        "snapshot_uri": bool(doorbell.snapshot_uri),
        "rtsp_describe": {
            "status": description.status if description else None,
            "medias": [asdict(media) for media in description.medias] if description else [],
        },
        "two_way_audio": doorbell.two_way_audio,
        "talk_codec": doorbell.talk_codec,
        "talk": doorbell.talk.as_dict() if doorbell.talk else None,
        "topics": doorbell.topics,
        "event_stream": {
            "connected": doorbell.events.connected,
            "filtered": doorbell.events.filtered,
            "topics": doorbell.events.topics,
            "last_error": doorbell.events.last_error,
        },
        "call": {
            "state": call.state,
            "ring_started": call.ring_started,
            "answered_by": call.answered_by,
            "last_ring": call.last_ring,
            "missed_calls": call.missed_calls,
        },
        "detections": doorbell.detections.state,
        "nvr": {
            "configured": doorbell.nvr_configured,
            "connected": doorbell.nvr is not None,
            "error": doorbell.nvr_error,
            "recordings": {
                quality: _hide_mac(recording.token, doorbell.mac)
                for quality, recording in (doorbell.nvr.recordings if doorbell.nvr else {}).items()
            },
            "visits": len(doorbell.visits.visits),
        },
        "last_events": list(doorbell.last_events),
    }
