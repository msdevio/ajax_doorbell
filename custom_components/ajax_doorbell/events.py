"""Keep an ONVIF PullPoint subscription alive and hand its events over."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .onvif import (
    AjaxOnvifClient,
    OnvifAuthError,
    OnvifConnectionError,
    OnvifError,
    OnvifEvent,
    PullPoint,
)

_LOGGER = logging.getLogger(__name__)

SUBSCRIPTION_SECONDS = 600
# PullMessages is a long poll: an event ends it at once, so this only bounds
# how long a dead connection can go unnoticed.
PULL_WAIT = 10
MAX_BACKOFF = 60


class EventStream:
    """One PullPoint subscription, re-created whenever it breaks."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AjaxOnvifClient,
        on_event: Callable[[OnvifEvent], None],
        on_connection: Callable[[bool], None],
        on_auth_error: Callable[[], None],
    ) -> None:
        """Initialize the stream."""
        self._hass = hass
        self._client = client
        self._on_event = on_event
        self._on_connection = on_connection
        self._on_auth_error = on_auth_error
        self._task: asyncio.Task | None = None
        self.connected = False
        self.last_error: str | None = None

    def start(self, entry: ConfigEntry) -> None:
        """Run the subscription loop for the life of the config entry."""
        self._task = entry.async_create_background_task(
            self._hass, self._run(), f"ajax_doorbell events {self._client.host}"
        )

    async def async_stop(self) -> None:
        """Stop the loop; the subscription is released on the way out."""
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _set_connected(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            self._on_connection(connected)

    async def _run(self) -> None:
        backoff = 1
        auth_retry = True
        while True:
            pullpoint: PullPoint | None = None
            try:
                pullpoint = await self._client.async_create_pullpoint(SUBSCRIPTION_SECONDS)
                with suppress(OnvifError):
                    await self._client.async_set_synchronization_point(pullpoint)
                if not self.connected:
                    _LOGGER.info("%s: listening for doorbell events", self._client.host)
                self._set_connected(True)
                self.last_error = None
                backoff, auth_retry = 1, True
                await self._pull_until_broken(pullpoint)
            except OnvifAuthError as err:
                self.last_error = str(err)
                self._set_connected(False)
                if auth_retry:
                    # A drifting device clock also fails WS-Security: resync once.
                    auth_retry = False
                    with suppress(OnvifError):
                        await self._client.async_update_time_offset()
                    continue
                _LOGGER.error("%s: the ONVIF account was refused", self._client.host)
                self._on_auth_error()
                return
            except OnvifError as err:
                if self.connected or self.last_error is None:
                    _LOGGER.log(
                        logging.INFO if isinstance(err, OnvifConnectionError) else logging.WARNING,
                        "Doorbell event stream interrupted: %s",
                        err,
                    )
                self.last_error = str(err)
                self._set_connected(False)
            finally:
                if pullpoint is not None:
                    with suppress(OnvifError, TimeoutError):
                        async with asyncio.timeout(5):
                            await self._client.async_unsubscribe(pullpoint)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _pull_until_broken(self, pullpoint: PullPoint) -> None:
        loop = asyncio.get_running_loop()
        renew_at = loop.time() + SUBSCRIPTION_SECONDS / 2
        while True:
            for event in await self._client.async_pull(pullpoint, PULL_WAIT):
                try:
                    self._on_event(event)
                except Exception:
                    _LOGGER.exception("Error handling doorbell event %s", event.topic)
            if loop.time() >= renew_at:
                try:
                    await self._client.async_renew(pullpoint, SUBSCRIPTION_SECONDS)
                except OnvifAuthError:
                    raise
                except OnvifError as err:
                    _LOGGER.debug("Renew refused (%s), creating a new subscription", err)
                    return
                renew_at = loop.time() + SUBSCRIPTION_SECONDS / 2
