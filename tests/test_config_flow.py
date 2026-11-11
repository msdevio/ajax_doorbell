"""Tests for the config flow."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ajax_doorbell.const import (
    CONF_CALL_TIMEOUT,
    CONF_RING_DURATION,
    DOMAIN,
)
from custom_components.ajax_doorbell.onvif import (
    OnvifAuthError,
    OnvifConnectionError,
    Recording,
)
from homeassistant.config_entries import SOURCE_DHCP, SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .conftest import ENTRY_DATA, HOST, MAC

DHCP = DhcpServiceInfo(ip=HOST, hostname="", macaddress="9c756e123456")


@pytest.fixture(autouse=True)
def no_setup() -> Generator[AsyncMock]:
    """Do not set the entry up after the flow."""
    with patch("custom_components.ajax_doorbell.async_setup_entry", return_value=True) as setup:
        yield setup


async def test_user_flow(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """The happy path creates the entry, keyed by the MAC."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ajax DoorBell"
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == MAC


@pytest.mark.parametrize(
    ("attribute", "effect", "error"),
    [
        ("async_get_profiles", OnvifAuthError("no"), "invalid_auth"),
        ("async_update_time_offset", OnvifConnectionError("down"), "cannot_connect"),
        ("async_get_profiles", [], "no_profiles"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, mock_client: MagicMock, attribute: str, effect, error: str
) -> None:
    """Errors are shown, and the flow recovers."""
    method = getattr(mock_client, attribute)
    working = method.return_value
    if isinstance(effect, Exception):
        method.side_effect = effect
    else:
        method.return_value = effect
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    method.side_effect = None
    method.return_value = working
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_dhcp_discovery(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """A discovered DoorBell only needs the ONVIF account."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == MAC


async def test_dhcp_ignores_the_hub(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """An Ajax MAC without ONVIF (the hub) is ignored."""
    mock_client.async_update_time_offset.side_effect = OnvifConnectionError("no onvif")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_video_device"


async def test_dhcp_offers_the_nvr(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """The NVR shares the Ajax prefix and ONVIF, plus recording services: offered as an NVR."""
    mock_client.async_get_services.return_value = {"device": "x", "search": "y", "replay": "z"}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "nvr"
    mock_client.async_get_services.assert_awaited_with(auth=False)


async def test_dhcp_updates_the_address(
    hass: HomeAssistant, mock_client: MagicMock, mock_entry: MockConfigEntry
) -> None:
    """A known DoorBell on a new IP gets its address updated."""
    mock_entry.add_to_hass(hass)
    new_ip = DhcpServiceInfo(ip="192.168.128.77", hostname="", macaddress="9c756e123456")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=new_ip
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_entry.data[CONF_HOST] == "192.168.128.77"


async def test_reauth(
    hass: HomeAssistant, mock_client: MagicMock, mock_entry: MockConfigEntry
) -> None:
    """A new password brings the entry back."""
    mock_entry.add_to_hass(hass)
    result = await mock_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    mock_client.async_get_profiles.side_effect = OnvifAuthError("no")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "homeassistant", CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    mock_client.async_get_profiles.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "homeassistant", CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_entry.data[CONF_PASSWORD] == "new"


async def test_reconfigure(
    hass: HomeAssistant, mock_client: MagicMock, mock_entry: MockConfigEntry
) -> None:
    """The address changes only when it is the same DoorBell."""
    mock_entry.add_to_hass(hass)
    new = {CONF_HOST: "192.168.128.40", CONF_PORT: 8080}
    mock_client.async_get_mac.return_value = "9c:75:6e:00:00:01"
    result = await mock_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new)
    assert result["reason"] == "wrong_device"
    mock_client.async_get_mac.return_value = MAC
    result = await mock_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new)
    assert result["reason"] == "reconfigure_successful"
    assert mock_entry.data[CONF_HOST] == "192.168.128.40"


async def test_options(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Ring and call timings are options."""
    mock_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RING_DURATION: 45, CONF_CALL_TIMEOUT: 600}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options[CONF_RING_DURATION] == 45
    assert mock_entry.options[CONF_CALL_TIMEOUT] == 600
    assert mock_entry.options["visit_retention"] == 2
    assert not mock_entry.options.get("nvr_host")


@pytest.mark.parametrize(
    ("services", "recordings", "error"),
    [
        ({"search": "s", "replay": "r"}, ["bcFhIz1NqO-9c756e123456-0-main-r"], None),
        ({"media2": "m"}, [], "nvr_not_nvr"),
        ({"search": "s", "replay": "r"}, ["a-0-main-r", "b-0-main-r"], "nvr_no_recording"),
    ],
)
async def test_options_nvr(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    mock_client: MagicMock,
    services: dict[str, str],
    recordings: list[str],
    error: str | None,
) -> None:
    """An NVR address is checked: Profile G, and a recording of this DoorBell."""
    mock_entry.add_to_hass(hass)
    mock_client.async_get_services.return_value = services
    mock_client.async_find_recordings = AsyncMock(
        return_value=[Recording(token, "Sonnette", None, None) for token in recordings]
    )
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_RING_DURATION: 30,
            CONF_CALL_TIMEOUT: 300,
            "nvr_host": " 192.168.128.30 ",
            "visit_retention": 2,
        },
    )
    if error is None:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert mock_entry.options["nvr_host"] == "192.168.128.30"
    else:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}


async def test_options_nvr_refuses_the_account(
    hass: HomeAssistant, mock_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """An NVR without the ONVIF user says so."""
    mock_entry.add_to_hass(hass)
    mock_client.async_get_services.side_effect = OnvifAuthError("no")
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_RING_DURATION: 30,
            CONF_CALL_TIMEOUT: 300,
            "nvr_host": "192.168.128.30",
            "visit_retention": 2,
        },
    )
    assert result["errors"] == {"base": "nvr_invalid_auth"}


async def test_user_adds_the_nvr(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """The NVR's address gets an NVR entry: its cameras will get their devices."""
    mock_client.async_get_services.return_value = {"device": "d", "search": "s", "replay": "r"}
    mock_client.async_get_mac.return_value = "9c:75:6e:00:00:30"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch("custom_components.ajax_doorbell.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**ENTRY_DATA, CONF_HOST: "192.168.128.30"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ajax NVR"
    assert result["data"] == {**ENTRY_DATA, CONF_HOST: "192.168.128.30", "kind": "nvr"}
    assert result["result"].unique_id == "9c:75:6e:00:00:30"
    assert result["result"].supports_options  # how long camera detections stay listed
