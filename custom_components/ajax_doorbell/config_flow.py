"""Config flow: find the DoorBell (or the NVR), check its ONVIF account, keep its address."""

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
    CONF_KIND,
    CONF_NVR_HOST,
    CONF_NVR_PASSWORD,
    CONF_NVR_PORT,
    CONF_NVR_USERNAME,
    CONF_RING_DURATION,
    CONF_VISIT_RETENTION,
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_ONVIF_PORT,
    DEFAULT_RING_DURATION,
    DEFAULT_USERNAME,
    DEFAULT_VISIT_RETENTION,
    DOMAIN,
    KIND_NVR,
    TOPIC_RING,
    is_nvr_entry,
)
from .doorbell import normalize_topic
from .nvr import NvrError, pick_recordings
from .onvif import AjaxOnvifClient, DeviceIdentity, OnvifAuthError, OnvifError

_LOGGER = logging.getLogger(__name__)

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))


class NvrAddressError(Exception):
    """The address is the NVR's: it gets an NVR entry, with its cameras."""


class NotNvrError(Exception):
    """The device has no recording services: it is not an NVR."""


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
        services = await client.async_get_services()
    except OnvifAuthError:
        raise
    except OnvifError as err:
        _LOGGER.debug("GetServices failed (%s), using default paths", err)
    else:
        if {"search", "replay"} <= services.keys():
            raise NvrAddressError
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


async def async_probe_nvr(
    hass: HomeAssistant, host: str, port: int, username: str, password: str
) -> tuple[DeviceIdentity, str | None]:
    """Log in to an NVR and read its identity and MAC address."""
    client = AjaxOnvifClient(async_get_clientsession(hass), host, port, username, password)
    await client.async_update_time_offset()
    services = await client.async_get_services()
    if not {"search", "replay"} <= services.keys():
        raise NotNvrError
    identity = await client.async_get_identity()
    mac = None
    with suppress(OnvifError):
        mac = await client.async_get_mac()
    return identity, mac


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

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """Only DoorBells have options (call timings, their NVR)."""
        return not is_nvr_entry(config_entry)

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
        except NvrAddressError:
            return None, {"base": "is_nvr"}
        except OnvifError as err:
            _LOGGER.debug("Cannot reach %s: %s", user_input[CONF_HOST], err)
            return None, {"base": "cannot_connect"}
        return result, {}

    async def _async_check_nvr(self, data: dict[str, Any]) -> tuple[str | None, dict[str, str]]:
        """Return the NVR's unique id, or the errors to show."""
        try:
            identity, mac = await async_probe_nvr(
                self.hass,
                data[CONF_HOST],
                data[CONF_PORT],
                data[CONF_USERNAME],
                data[CONF_PASSWORD],
            )
        except OnvifAuthError:
            return None, {"base": "invalid_auth"}
        except NotNvrError:
            return None, {"base": "not_nvr"}
        except OnvifError as err:
            _LOGGER.debug("Cannot reach the NVR at %s: %s", data[CONF_HOST], err)
            return None, {"base": "cannot_connect"}
        return (format_mac(mac) if mac else self._mac or identity.serial), {}

    async def _async_finish_nvr(self, unique_id: str, data: dict[str, Any]) -> ConfigFlowResult:
        await self.async_set_unique_id(unique_id, raise_on_progress=False)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: data[CONF_HOST], CONF_PORT: data[CONF_PORT]}
        )
        return self.async_create_entry(title="Ajax NVR", data={**data, CONF_KIND: KIND_NVR})

    def _known_nvr_account(self, host: str) -> tuple[str, str] | None:
        """Return the account a DoorBell's options already use for this NVR."""
        for entry in self._async_current_entries(include_ignore=False):
            options = entry.options
            if is_nvr_entry(entry) or (options.get(CONF_NVR_HOST) or "").strip() != host:
                continue
            return (
                options.get(CONF_NVR_USERNAME) or entry.data[CONF_USERNAME],
                options.get(CONF_NVR_PASSWORD) or entry.data[CONF_PASSWORD],
            )
        return None

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """Handle a device with the Ajax MAC prefix.

        The hub shares the prefix but has no ONVIF service: an unauthenticated
        clock request tells the two apart without needing any account. The NVR
        has ONVIF too, but also the recording services a DoorBell lacks: it is
        offered as an NVR, whose cameras each get a device.
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
        try:
            services = await client.async_get_services(auth=False)
        except OnvifError:
            services = {}  # some firmwares want an account here: offer the device
        self._host, self._mac = discovery_info.ip, mac
        if {"search", "replay"} & services.keys():
            self.context["title_placeholders"] = {"name": f"Ajax NVR ({discovery_info.ip})"}
            return await self.async_step_nvr()
        self.context["title_placeholders"] = {"name": f"Ajax ({discovery_info.ip})"}
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the address and the ONVIF account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            result, errors = await self._async_check(user_input)
            if errors.get("base") == "is_nvr":
                # The NVR's address: it gets its own entry, and its cameras their devices.
                unique_id, errors = await self._async_check_nvr(user_input)
                if unique_id is not None:
                    return await self._async_finish_nvr(unique_id, user_input)
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

    async def async_step_nvr(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add the NVR found on the network, with its ONVIF account."""
        assert self._host is not None
        known = self._known_nvr_account(self._host)
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input.get(CONF_PASSWORD) or ""
            if not password and known and known[0] == username:
                password = known[1]  # the one saved in the DoorBell's options
            data = {
                CONF_HOST: self._host,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
                CONF_PORT: DEFAULT_ONVIF_PORT,
            }
            if not password:
                errors["base"] = "password_required"
            else:
                unique_id, errors = await self._async_check_nvr(data)
                if unique_id is not None:
                    return await self._async_finish_nvr(unique_id, data)
        return self.async_show_form(
            step_id="nvr",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=known[0] if known else DEFAULT_USERNAME
                    ): str,
                    vol.Optional(CONF_PASSWORD): PASSWORD,
                }
            ),
            description_placeholders={"host": self._host},
            errors=errors,
        )

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
            if is_nvr_entry(entry):
                _, errors = await self._async_check_nvr({**entry.data, **user_input})
            else:
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
            if is_nvr_entry(entry):
                unique_id, errors = await self._async_check_nvr({**entry.data, **user_input})
                if unique_id is not None:
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                    return self.async_update_reload_and_abort(entry, data_updates=user_input)
                result = None
            else:
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


async def async_check_nvr(
    hass: HomeAssistant, host: str, port: int, username: str, password: str, mac: str | None
) -> str | None:
    """Return an error key, or None when the NVR records this DoorBell."""
    client = AjaxOnvifClient(async_get_clientsession(hass), host, port, username, password)
    try:
        await client.async_update_time_offset()
        services = await client.async_get_services()
        if not {"search", "replay"} <= services.keys():
            return "nvr_not_nvr"
        pick_recordings(await client.async_find_recordings(), mac)
    except OnvifAuthError:
        return "nvr_invalid_auth"
    except OnvifError:
        return "nvr_cannot_connect"
    except NvrError:
        return "nvr_no_recording"
    return None


class AjaxDoorbellOptionsFlow(OptionsFlowWithReload):
    """Call timings and the NVR; saving them reloads the DoorBell."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options."""
        errors: dict[str, str] = {}
        entry = self.config_entry
        if user_input is not None:
            if host := (user_input.get(CONF_NVR_HOST) or "").strip():
                user_input[CONF_NVR_HOST] = host
                mac = getattr(getattr(entry, "runtime_data", None), "mac", None)
                if error := await async_check_nvr(
                    self.hass,
                    host,
                    int(user_input.get(CONF_NVR_PORT) or DEFAULT_ONVIF_PORT),
                    user_input.get(CONF_NVR_USERNAME) or entry.data[CONF_USERNAME],
                    user_input.get(CONF_NVR_PASSWORD) or entry.data[CONF_PASSWORD],
                    mac,
                ):
                    errors["base"] = error
            if not errors:
                return self.async_create_entry(data=user_input)
        options = {**entry.options, **(user_input or {})}

        def suggested(key: str) -> dict[str, Any]:
            value = options.get(key)
            return {"suggested_value": value} if value not in (None, "") else {}

        return self.async_show_form(
            step_id="init",
            errors=errors,
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
                    vol.Optional(
                        CONF_NVR_HOST, description=suggested(CONF_NVR_HOST)
                    ): TextSelector(),
                    vol.Optional(
                        CONF_NVR_PORT, default=options.get(CONF_NVR_PORT, DEFAULT_ONVIF_PORT)
                    ): NumberSelector(
                        NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
                    ),
                    vol.Optional(
                        CONF_NVR_USERNAME, description=suggested(CONF_NVR_USERNAME)
                    ): TextSelector(),
                    vol.Optional(
                        CONF_NVR_PASSWORD, description=suggested(CONF_NVR_PASSWORD)
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                    vol.Required(
                        CONF_VISIT_RETENTION,
                        default=options.get(CONF_VISIT_RETENTION, DEFAULT_VISIT_RETENTION),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1,
                            max=30,
                            step=1,
                            unit_of_measurement="d",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
