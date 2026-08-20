"""Component to embed TP-Link smart home devices."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging

from kasa import AuthenticationError, Credentials, Device, KasaException
from kasa.iot import IotStrip

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class _ModuleQueryErrorFilter(logging.Filter):
    """Demote python-kasa's per-module query errors from ERROR to DEBUG.

    kasa.smart.smartdevice logs its own ERROR for each module that times out
    during update() (e.g. "Error querying <ip> for modules 'Time, AutoOff,
    ..., Matter' after first update"). These are logged *inside* device.update()
    and never propagate as exceptions when the overall update still returns, so
    the coordinator's own error handling never sees them — they just spam the
    log. When the whole update actually fails, the coordinator raises
    UpdateFailed with its own (single, meaningful) message instead.

    This filter keeps those transient per-module lines out of the user-facing
    log while leaving genuine failures visible via the coordinator's warnings.
    """

    _NEEDLE = "after first update"

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and self._NEEDLE in record.getMessage():
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        return True


logging.getLogger("kasa.smart.smartdevice").addFilter(_ModuleQueryErrorFilter())

# Maximum seconds to wait for a single device.update() call.
# If the ES20M hangs mid-connection this cap ensures the lock is
# released so future polls can still run instead of being blocked forever.
#
# NOTE: SMART/KLAP devices (P1xx, KS2xx, L5xx, Matter-capable) query several
# modules per update (Time, AutoOff, Cloud, DeviceModule, Matter) over an
# encrypted handshake and are noticeably slower than the IOT ES20M. 15s was
# too tight for those and caused wait_for to trip during overnight Wi-Fi
# congestion, so the cap is raised to 25s.
UPDATE_TIMEOUT = 25

# FIX 4: Consecutive-failure backoff.
# After this many straight failures the coordinator skips extra poll cycles
# so the ES20M gets breathing room instead of being hammered while struggling.
# The skip count doubles with each additional failure up to MAX_BACKOFF_SKIPS.
BACKOFF_THRESHOLD = 3    # failures before backing off
BACKOFF_SKIP_COUNT = 2   # base skip cycles at threshold (doubles each extra failure)
MAX_BACKOFF_SKIPS = 8    # hard cap (~4 min at 30s interval)

# FIX 7: Retry-once-before-failing.
# Many ES20M connection resets are transient — the device drops one connection
# but answers cleanly on an immediate second attempt. Doing one quick retry
# within the same poll cycle means the entity never flips to unavailable for
# these blips, cutting visible failures significantly.
RETRY_BACKOFF_DELAY = 1.0  # seconds to wait before the single retry


@dataclass(slots=True)
class TPLinkData:
    """Data for the tplink integration."""

    parent_coordinator: TPLinkDataUpdateCoordinator
    camera_credentials: Credentials | None
    live_view: bool | None


type TPLinkConfigEntry = ConfigEntry[TPLinkData]

REQUEST_REFRESH_DELAY = 0.35


class TPLinkDataUpdateCoordinator(DataUpdateCoordinator[None]):
    """DataUpdateCoordinator to gather data for a specific TPLink device."""

    config_entry: TPLinkConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        device: Device,
        update_interval: timedelta,
        config_entry: TPLinkConfigEntry,
    ) -> None:
        """Initialize DataUpdateCoordinator to gather data for specific SmartPlug."""
        self.device = device

        # The iot HS300 allows a limited number of concurrent requests and
        # fetching the emeter information requires separate ones, so child
        # coordinators are created below in get_child_coordinator.
        self._update_children = not isinstance(device, IotStrip)

        # FIX 1: Lock to prevent overlapping poll cycles.
        # If a poll is still in progress when the next interval fires,
        # the new poll is skipped entirely to avoid stacking requests
        # on the device (a primary cause of ES20M freezing).
        self._update_lock = asyncio.Lock()

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=device.host,
            update_interval=update_interval,
            # We don't want an immediate refresh since the device
            # takes a moment to reflect the state change
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=REQUEST_REFRESH_DELAY, immediate=False
            ),
        )
        self._previous_child_device_ids = {child.device_id for child in device.children}
        self.removed_child_device_ids: set[str] = set()
        self._child_coordinators: dict[str, TPLinkDataUpdateCoordinator] = {}

        # FIX 4: Consecutive failure / backoff tracking.
        self._consecutive_failures: int = 0
        self._backoff_skips_remaining: int = 0

    async def _async_update_data(self) -> None:
        """Fetch all device and sensor data from api."""

        # FIX 4: Skip extra cycles when the device has been repeatedly failing.
        if self._backoff_skips_remaining > 0:
            self._backoff_skips_remaining -= 1
            _LOGGER.debug(
                "Backing off poll for %s — %d skip(s) remaining after repeated failures",
                self.device.host,
                self._backoff_skips_remaining,
            )
            return

        # FIX 1: Skip this poll cycle if the previous one is still running.
        # Without this guard, a slow/hanging device.update() causes polls
        # to stack up, overwhelming the ES20M's limited TCP stack.
        if self._update_lock.locked():
            _LOGGER.debug(
                "Skipping poll for %s — previous update still in progress",
                self.device.host,
            )
            return

        async with self._update_lock:
            try:
                # FIX 7: Attempt the update with a single retry on transient errors.
                # The first attempt may hit a transient reset/timeout; if so we
                # close the stale connection, pause briefly, and try once more
                # before propagating the failure to the coordinator.
                await self._update_with_retry()
            except asyncio.TimeoutError as ex:
                await self._close_protocol()
                self._record_failure()
                _LOGGER.warning(
                    "Update timed out for %s after %ds (failure #%d)",
                    self.device.host,
                    UPDATE_TIMEOUT,
                    self._consecutive_failures,
                )
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="device_error",
                    translation_placeholders={
                        "func": "update",
                        "exc": f"Timed out after {UPDATE_TIMEOUT}s",
                    },
                ) from ex
            except AuthenticationError as ex:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="device_authentication",
                    translation_placeholders={
                        "func": "update",
                        "exc": str(ex),
                    },
                ) from ex
            except KasaException as ex:
                # FIX 2: On any Kasa error, proactively close the protocol
                # connection so the next poll starts fresh instead of reusing
                # a stale/broken TCP connection that may hang indefinitely.
                await self._close_protocol()
                self._record_failure()
                # FIX 6: Log the actual exception type + message so the root
                # cause is visible instead of the generic "device_error" key.
                _LOGGER.warning(
                    "Update failed for %s (failure #%d): %s: %s",
                    self.device.host,
                    self._consecutive_failures,
                    type(ex).__name__,
                    ex,
                )
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="device_error",
                    translation_placeholders={
                        "func": "update",
                        "exc": str(ex),
                    },
                ) from ex
            else:
                # FIX 2: Close the connection after every successful poll.
                # The ES20M has a very limited TCP stack and cannot reliably
                # maintain a persistent idle connection between poll cycles.
                # Closing after each poll forces a clean reconnect next time,
                # preventing stale connection hangs and device freezes.
                await self._close_protocol()
                # FIX 4: Reset failure counter on a clean poll.
                if self._consecutive_failures > 0:
                    _LOGGER.info(
                        "Poll recovered for %s after %d consecutive failure(s)",
                        self.device.host,
                        self._consecutive_failures,
                    )
                self._consecutive_failures = 0

        await self._process_child_devices()

    async def _update_with_retry(self) -> None:
        """Run device.update() with a single retry on transient errors.

        FIX 7: The first attempt is wrapped in the FIX 3 hard timeout. If it
        raises a transient KasaException or times out, the stale connection is
        closed, a short pause is taken, and exactly one more attempt is made.
        Only if that second attempt also fails does the error propagate to the
        caller (which then records the failure and raises UpdateFailed).

        Authentication errors are NOT retried — a bad credential won't fix
        itself, so it's re-raised immediately for the auth-failed handler.
        """
        for attempt in (1, 2):
            try:
                # FIX 3: Hard timeout on the update call so a hung connection
                # can never hold the lock open indefinitely.
                await asyncio.wait_for(
                    self.device.update(update_children=self._update_children),
                    timeout=UPDATE_TIMEOUT,
                )
                if attempt == 2:
                    _LOGGER.debug(
                        "Update for %s succeeded on retry", self.device.host
                    )
                return
            except AuthenticationError:
                # Not transient — let the caller handle it immediately.
                raise
            except (asyncio.TimeoutError, KasaException) as ex:
                if attempt == 1:
                    # Transient failure: close the stale connection, pause,
                    # and fall through to the second attempt.
                    _LOGGER.debug(
                        "Update for %s failed on attempt 1 (%s) — retrying once",
                        self.device.host,
                        type(ex).__name__,
                    )
                    await self._close_protocol()
                    await asyncio.sleep(RETRY_BACKOFF_DELAY)
                    continue
                # Second attempt also failed — propagate to the caller.
                raise

    def _record_failure(self) -> None:
        """Track consecutive failures and engage progressive backoff if threshold is reached."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= BACKOFF_THRESHOLD:
            # Progressive backoff: double the skip count with each failure beyond
            # the threshold, capped at MAX_BACKOFF_SKIPS.
            # e.g. at 30s interval: 3 failures=60s, 4=120s, 5+=240s
            extra = self._consecutive_failures - BACKOFF_THRESHOLD
            skip = min(BACKOFF_SKIP_COUNT * (2**extra), MAX_BACKOFF_SKIPS)
            self._backoff_skips_remaining = skip
            _LOGGER.warning(
                "%s has failed %d consecutive poll(s) — backing off for %d cycle(s)",
                self.device.host,
                self._consecutive_failures,
                skip,
            )

    async def _close_protocol(self) -> None:
        """Safely close the device protocol connection."""
        try:
            await self.device.protocol.close()
        except Exception as ex:  # noqa: BLE001
            _LOGGER.debug(
                "Ignoring error while closing protocol for %s: %s",
                self.device.host,
                ex,
            )

    async def _process_child_devices(self) -> None:
        """Process child devices and remove stale devices."""
        current_child_device_ids = {child.device_id for child in self.device.children}
        if (
            stale_device_ids := self._previous_child_device_ids
            - current_child_device_ids
        ):
            device_registry = dr.async_get(self.hass)
            for device_id in stale_device_ids:
                device = device_registry.async_get_device(
                    identifiers={(DOMAIN, device_id)}
                )
                if device:
                    device_registry.async_update_device(
                        device_id=device.id,
                        remove_config_entry_id=self.config_entry.entry_id,
                    )
                child_coordinator = self._child_coordinators.pop(device_id, None)
                if child_coordinator:
                    await child_coordinator.async_shutdown()

        self._previous_child_device_ids = current_child_device_ids
        self.removed_child_device_ids = stale_device_ids

    def get_child_coordinator(
        self,
        child: Device,
        platform_domain: str,
    ) -> TPLinkDataUpdateCoordinator:
        """Get separate child coordinator for a device or self if not needed."""
        # The iot HS300 allows a limited number of concurrent requests and fetching the
        # emeter information requires separate ones so create child coordinators here.
        # This does not happen for switches as the state is available on the
        # parent device info.
        if isinstance(self.device, IotStrip) and platform_domain != SWITCH_DOMAIN:
            if not (child_coordinator := self._child_coordinators.get(child.device_id)):
                # The child coordinators only update energy data so we can
                # set a longer update interval to avoid flooding the device
                child_coordinator = TPLinkDataUpdateCoordinator(
                    self.hass, child, timedelta(seconds=60), self.config_entry
                )
                self._child_coordinators[child.device_id] = child_coordinator
            return child_coordinator

        return self
