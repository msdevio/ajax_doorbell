"""Keep an ONVIF PullPoint subscription alive and hand its events over."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
# After a refusal the clock is read again and the subscription retried this soon.
RESYNC_DELAY = 1
# A device that keeps refusing is retried this slowly: it may recover by itself
# (reboot), and hammering it with a wrong password helps nobody.
REFUSED_DELAY = 300
# The DoorBell repeats its states every few seconds, so a filtered subscription
# that stays silent this long filters too much: it is dropped for a plain one.
FILTER_SILENCE = 60


class EventStream:
    """One PullPoint subscription, re-created whenever it breaks."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AjaxOnvifClient,
        on_event: Callable[[OnvifEvent], None],
        on_connection: Callable[[bool], None],
        on_refused: Callable[[], Awaitable[None]],
        on_recovered: Callable[[], None],
    ) -> None:
        """Initialize the stream."""
        self._hass = hass
        self._client = client
        self._on_event = on_event
        self._on_connection = on_connection
        self._on_refused = on_refused
        self._on_recovered = on_recovered
        self._task: asyncio.Task | None = None
        self._backoff = 1
        self._refusals = 0
        self._refusal_reported = False
        # Topics to subscribe to; empty, or once the device mishandles the filter, all of them.
        self.topics: list[str] = []
        self.filtered = False
        self._filter_failed = False
        self.connected = False
        self.last_error: str | None = None

    def start(self, entry: ConfigEntry) -> None:
        """Run the subscription loop for the life of the config entry."""
        self._task = entry.async_create_background_task(
            self._hass, self._run(), f"ajax_doorbell events {self._client.host}"
        )

    async def async_stop(self) -> None:
        """Stop the loop; the subscription is released on the way out."""
        if (task := self._task) is None:
            return
        self._task = None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Only swallow the cancellation we asked for, not our caller's.
            if (current := asyncio.current_task()) is not None and current.cancelling():
                raise
        self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            self._on_connection(connected)

    def _healthy(self) -> None:
        """The first pull of a subscription worked: the stream is really up."""
        self._backoff = 1
        self._refusals = 0
        self.last_error = None
        if self._refusal_reported:
            self._refusal_reported = False
            self._on_recovered()
        if not self.connected:
            _LOGGER.info("%s: listening for doorbell events", self._client.host)
        self._set_connected(True)

    async def _run(self) -> None:
        while True:
            try:
                # A fresh subscription: at once without the filter, else soon
                delay = 0 if await self._subscribe_and_pull() else 1
            except OnvifAuthError as err:
                self.last_error = str(err)
                self._set_connected(False)
                self._refusals += 1
                if self._refusals == 1:
                    # A drifting device clock also fails WS-Security: read it again.
                    with suppress(OnvifError):
                        await self._client.async_update_time_offset()
                    delay = RESYNC_DELAY
                else:
                    if not self._refusal_reported:
                        self._refusal_reported = True
                        _LOGGER.warning(
                            "%s: the doorbell refuses the ONVIF account for events",
                            self._client.host,
                        )
                        await self._on_refused()
                    delay = REFUSED_DELAY
            except OnvifError as err:
                if self.connected or self.last_error is None:
                    _LOGGER.log(
                        logging.INFO if isinstance(err, OnvifConnectionError) else logging.WARNING,
                        "Doorbell event stream interrupted: %s",
                        err,
                    )
                self.last_error = str(err)
                self._set_connected(False)
                delay = self._backoff
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)
            except Exception:
                _LOGGER.exception("Unexpected error in the doorbell event stream")
                self.last_error = "unexpected error"
                self._set_connected(False)
                delay = MAX_BACKOFF
            await asyncio.sleep(delay)

    async def _subscribe(self) -> PullPoint:
        """Subscribe to the wanted topics, or to everything if the filter is refused."""
        self.filtered = bool(self.topics) and not self._filter_failed
        if self.filtered:
            try:
                return await self._client.async_create_pullpoint(SUBSCRIPTION_SECONDS, self.topics)
            except (OnvifAuthError, OnvifConnectionError):
                raise
            except OnvifError as err:
                _LOGGER.debug("%s refuses the topic filter: %s", self._client.host, err)
                self._filter_failed = True
                self.filtered = False
        return await self._client.async_create_pullpoint(SUBSCRIPTION_SECONDS)

    async def _subscribe_and_pull(self) -> bool:
        pullpoint: PullPoint | None = None
        try:
            pullpoint = await self._subscribe()
            with suppress(OnvifError):
                await self._client.async_set_synchronization_point(pullpoint)
            return await self._pull_until_broken(pullpoint)
        finally:
            if pullpoint is not None:
                with suppress(OnvifError, TimeoutError):
                    async with asyncio.timeout(5):
                        await self._client.async_unsubscribe(pullpoint)

    async def _pull_until_broken(self, pullpoint: PullPoint) -> bool:
        """Pull until the subscription ends; True when the filter was just dropped."""
        loop = asyncio.get_running_loop()
        renew_at = loop.time() + SUBSCRIPTION_SECONDS / 2
        silent_until = loop.time() + FILTER_SILENCE if self.filtered else None
        first = True
        while True:
            events = await self._client.async_pull(pullpoint, PULL_WAIT)
            if first:
                first = False
                self._healthy()
            if silent_until is not None:
                if events:
                    silent_until = None
                elif loop.time() >= silent_until:
                    _LOGGER.debug(
                        "%s: nothing through the topic filter, dropping it", self._client.host
                    )
                    self._filter_failed = True
                    return True
            for event in events:
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
                    return False
                renew_at = loop.time() + SUBSCRIPTION_SECONDS / 2
