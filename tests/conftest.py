"""Fixtures for the Ajax DoorBell tests."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch
import xml.etree.ElementTree as ET

import pytest

# The camera component imports turbojpeg, a native library the test machines
# do not have; stub it so the platform imports.
if "turbojpeg" not in sys.modules:
    try:
        import turbojpeg  # noqa: F401
    except ImportError:
        _turbojpeg = types.ModuleType("turbojpeg")
        _turbojpeg.TurboJPEG = type("TurboJPEG", (), {"__init__": lambda self, *a, **k: None})
        sys.modules["turbojpeg"] = _turbojpeg
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ajax_doorbell.const import DOMAIN
from custom_components.ajax_doorbell.onvif import (
    DeviceIdentity,
    parse_profiles,
    parse_topics,
)
from custom_components.ajax_doorbell.rtsp import SdpMedia, StreamDescription
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "192.168.128.31"
MAC = "9c:75:6e:12:34:56"
ENTRY_DATA = {
    CONF_HOST: HOST,
    CONF_PORT: 8080,
    CONF_USERNAME: "homeassistant",
    CONF_PASSWORD: "secret",
}
IDENTITY = DeviceIdentity("AJAX", "oa8000-nand", "2.669-1", "AJX12345")

# What the DoorBell is expected to answer to DESCRIBE with the backchannel
# requirement (to be confirmed by the phase 0 probe).
DESCRIPTION = StreamDescription(
    200,
    (
        SdpMedia("video", "recvonly", ("H264/90000",), "trackID=0"),
        SdpMedia("audio", "recvonly", ("G722/8000",), "trackID=1"),
        SdpMedia("audio", "sendonly", ("PCMA/8000", "PCMU/8000"), "trackID=2"),
    ),
)


def load_xml(name: str) -> ET.Element:
    """Parse a fixture file."""
    return ET.fromstring((FIXTURES / name).read_bytes())


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    """Return a config entry for the DoorBell."""
    return MockConfigEntry(
        domain=DOMAIN, title="Ajax DoorBell", unique_id=MAC, data=dict(ENTRY_DATA)
    )


def make_client() -> MagicMock:
    """Return an ONVIF client double that answers like the DoorBell."""
    client = MagicMock()
    client.host = HOST
    client.services = {}
    client.uses_http_digest = False
    client.time_offset = timedelta(0)
    client.async_update_time_offset = AsyncMock(return_value=timedelta(0))
    client.async_get_services = AsyncMock(return_value={})
    client.async_get_identity = AsyncMock(return_value=IDENTITY)
    client.async_get_mac = AsyncMock(return_value=MAC)
    client.async_get_profiles = AsyncMock(return_value=parse_profiles(load_xml("get_profiles.xml")))
    client.async_get_stream_uri = AsyncMock(
        side_effect=lambda token: (
            f"rtsp://{HOST}:8554/9c756e123456-0_{'m' if token == 'main_token' else 's'}"
        )
    )
    client.async_get_snapshot_uri = AsyncMock(return_value=None)
    client.async_get_topics = AsyncMock(
        return_value=parse_topics(load_xml("get_event_properties.xml"))
    )
    return client


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Replace the ONVIF client everywhere."""
    client = make_client()
    with (
        patch(
            "custom_components.ajax_doorbell.doorbell.AjaxOnvifClient",
            return_value=client,
        ),
        patch(
            "custom_components.ajax_doorbell.config_flow.AjaxOnvifClient",
            return_value=client,
        ),
    ):
        yield client


@pytest.fixture
def mock_describe() -> Generator[AsyncMock]:
    """Replace the RTSP DESCRIBE probe."""
    with patch(
        "custom_components.ajax_doorbell.doorbell.async_describe",
        AsyncMock(return_value=DESCRIPTION),
    ) as describe:
        yield describe


@pytest.fixture
def no_event_stream() -> Generator[None]:
    """Do not start the PullPoint loop; pretend it is connected."""

    def _start(self, entry) -> None:
        self._set_connected(True)

    with patch("custom_components.ajax_doorbell.events.EventStream.start", _start):
        yield
