"""The integration ships its own icon (brand/ folder)."""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_custom_components

BRAND = Path(__file__).parent.parent / "custom_components" / "ajax_doorbell" / "brand"


async def test_brand_images(hass: HomeAssistant) -> None:
    """Home Assistant sees the local brand images."""
    integration = (await async_get_custom_components(hass))["ajax_doorbell"]
    assert integration.has_branding
    for image in ("icon.png", "icon@2x.png", "dark_icon.png", "logo.png", "logo@2x.png"):
        assert (BRAND / image).read_bytes().startswith(b"\x89PNG")
