"""Diagnostics: what the DoorBell exposes, with secrets removed."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import AjaxDoorbellConfigEntry
from .rtsp import strip_credentials

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, "serial", "mac", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AjaxDoorbellConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
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
            token: strip_credentials(uri) for token, uri in doorbell.stream_uris.items()
        },
        "snapshot_uri": bool(doorbell.snapshot_uri),
        "rtsp_describe": {
            "status": description.status if description else None,
            "medias": [asdict(media) for media in description.medias] if description else [],
        },
        "two_way_audio": doorbell.two_way_audio,
        "topics": doorbell.topics,
        "event_stream": {
            "connected": doorbell.events.connected,
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
        "last_events": list(doorbell.last_events),
    }
