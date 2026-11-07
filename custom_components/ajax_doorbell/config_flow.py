"""Config flow: find the DoorBell, check its ONVIF account, keep its address."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .const import (
    CONF_CALL_TIMEOUT,
    CONF_RING_DURATION,
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_ONVIF_PORT,
    DEFAULT_RING_DURATION,
    DEFAULT_USERNAME,
    DOMAIN,
    TOPIC_RING,
)
from .doorbell import normalize_topic
from .onvif import AjaxOnvifClient, DeviceIdentity, OnvifAuthError, OnvifError

_LOGGER = logging.getLogger(__name__)

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))


class NoProfilesError(Exception):
    """The device answers ONVIF but exposes no video."""


@dataclass(slots=True)
class ProbeResult:
    """What the flow learns from the device."""

    identity: DeviceIdentity
    mac: str | None
    doorbell: bool


async def async_probe(
    hass: HomeAssistant, host: str, port: int, username: str, password: str
) -> ProbeResult:
    """Log in and make the calls the setup needs, so a flow that passes sets up."""
    client = AjaxOnvifClient(async_get_clientsession(hass), host, port, username, password)
    await client.async_update_time_offset()
    try:
        await client.async_get_services()
    except OnvifAuthError:
        raise
    except OnvifError as err:
        _LOGGER.debug("GetServices failed (%s), using default paths", err)
    identity = await client.async_get_identity()
    profiles = await client.async_get_profiles()
    if not profiles:
        raise NoProfilesError
    await client.async_get_stream_uri(max(profiles, key=lambda p: p.pixels).token)
    mac = None
    with suppress(OnvifError):
        mac = await client.async_get_mac()
    topics: list[str] = []
    with suppress(OnvifError):
        topics = await client.async_get_topics()
    doorbell = not topics or any(
        pattern in normalize_topic(topic) for topic in topics for pattern in TOPIC_RING
    )
    return ProbeResult(identity, mac, doorbell)


def _user_schema(host: str | None) -> vol.Schema:
    return vol.Schema(
        {
            (vol.Required(CONF_HOST, default=host) if host else vol.Required(CONF_HOST)): str,
            vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
            vol.Required(CONF_PASSWORD): PASSWORD,
            vol.Required(CONF_PORT, default=DEFAULT_ONVIF_PORT): PORT,
        }
    )


class AjaxDoorbellConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add an Ajax DoorBell."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing discovered."""
        self._host: str | None = None
        self._mac: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AjaxDoorbellOptionsFlow:
        """Return the options flow."""
        return AjaxDoorbellOptionsFlow()

    async def _async_check(
        self, user_input: dict[str, Any]
    ) -> tuple[ProbeResult | None, dict[str, str]]:
        try:
            result = await async_probe(
                self.hass,
                user_input[CONF_HOST],
                user_input[CONF_PORT],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
        except OnvifAuthError:
            return None, {"base": "invalid_auth"}
        except NoProfilesError:
            return None, {"base": "no_profiles"}
        except OnvifError as err:
            _LOGGER.debug("Cannot reach %s: %s", user_input[CONF_HOST], err)
            return None, {"base": "cannot_connect"}
        return result, {}

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Handle a device with the Ajax MAC prefix.

        The hub shares the prefix but has no ONVIF service: an unauthenticated
        clock request tells the two apart without needing any account.
        """
        mac = format_mac(discovery_info.macaddress)
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured(updates={CONF_HOST: discovery_info.ip})
        client = AjaxOnvifClient(
            async_get_clientsession(self.hass), discovery_info.ip, DEFAULT_ONVIF_PORT, "", ""
        )
        try:
            await client.async_update_time_offset()
        except OnvifError:
            return self.async_abort(reason="not_video_device")
        self._host, self._mac = discovery_info.ip, mac
        self.context["title_placeholders"] = {"name": f"Ajax ({discovery_info.ip})"}
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the address and the ONVIF account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            result, errors = await self._async_check(user_input)
            if result is not None:
                unique_id = (
                    format_mac(result.mac) if result.mac else self._mac or result.identity.serial
                )
                await self.async_set_unique_id(unique_id, raise_on_progress=False)
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input[CONF_PORT],
                    }
                )
                title = "Ajax DoorBell" if result.doorbell else f"Ajax {result.identity.model}"
                return self.async_create_entry(title=title, data=user_input)
        schema = _user_schema(self._host)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(
                schema, {k: v for k, v in user_input.items() if k != CONF_PASSWORD}
            )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The DoorBell refused the account."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the account again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            _, errors = await self._async_check({**entry.data, **user_input})
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)
                    ): str,
                    vol.Required(CONF_PASSWORD): PASSWORD,
                }
            ),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address or port of the same DoorBell."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            result, errors = await self._async_check({**entry.data, **user_input})
            if result is not None:
                if result.mac:
                    await self.async_set_unique_id(format_mac(result.mac))
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_HOST): str, vol.Required(CONF_PORT): PORT}),
                user_input or entry.data,
            ),
            errors=errors,
        )


class AjaxDoorbellOptionsFlow(OptionsFlowWithReload):
    """Ring and call timings; saving them reloads the DoorBell."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_RING_DURATION,
                        default=options.get(CONF_RING_DURATION, DEFAULT_RING_DURATION),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=10,
                            max=180,
                            step=5,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_CALL_TIMEOUT,
                        default=options.get(CONF_CALL_TIMEOUT, DEFAULT_CALL_TIMEOUT),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=60,
                            max=1800,
                            step=30,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
