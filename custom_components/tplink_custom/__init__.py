"""TP-Link Smart Home (Custom) integration."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import timedelta
import logging
from typing import Any

from aiohttp import ClientSession
from kasa import AuthenticationError, Credentials, Device, DeviceConfig, Discover, KasaException
from kasa.deviceconfig import DeviceConnectionParameters
from kasa.httpclient import get_cookie_jar

from homeassistant import config_entries
from homeassistant.components import network
from homeassistant.const import (
    CONF_AUTHENTICATION,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    CONF_AES_KEYS,
    CONF_CONNECTION_PARAMETERS,
    CONF_CREDENTIALS_HASH,
    CONF_ENABLE_UDP_DISCOVERY,
    CONF_USES_HTTP,
    CONNECT_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DISCOVERY_TIMEOUT,
    DOMAIN,
    PLATFORMS,
    SMART_BATCH_SIZE,
)
from .coordinator import TPLinkConfigEntry, TPLinkData, TPLinkDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

DISCOVERY_INTERVAL = timedelta(minutes=15)

# Fallback interval used only when no option is set and the device model
# is not listed in DEVICE_SCAN_INTERVALS below.
DEFAULT_UPDATE_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)

# Per-model safe defaults. These are used as the initial suggested value
# when the user opens Options for the first time, before they save anything.
# Once the user saves an interval via Options it takes full precedence.
DEVICE_SCAN_INTERVALS: dict[str, int] = {
    "ES20M": 30,  # Conservative interval for the ES20M's limited TCP stack
}

_DISCOVERY_UNSUB = "_discovery_unsub"


class _CameraDiscoveryErrorFilter(logging.Filter):
    """Drop the noisy KeyError(<DeviceType.Camera>) traceback from python-kasa.

    When UDP discovery is on and a TP-Link camera answers the broadcast,
    python-kasa's device factory has no mapping for DeviceType.Camera and
    raises KeyError inside an asyncio datagram callback. That callback is
    outside our await path, so it cannot be caught with try/except; it is
    only logged. This filter suppresses that one specific record and lets
    everything else through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info
        if exc and exc[0] is KeyError:
            # str(KeyError(DeviceType.Camera)) -> "<DeviceType.Camera: 'camera'>"
            if "DeviceType.Camera" in str(exc[1]):
                return False
        # Also catch cases where kasa formats it into the message itself.
        msg = record.getMessage()
        if "DeviceType.Camera" in msg and "_get_device_class" in msg:
            return False
        return True


_CAMERA_FILTER = _CameraDiscoveryErrorFilter()
# The KeyError surfaces via the asyncio callback machinery; kasa.discover is
# where it originates. Attach to both so we catch it regardless of logger.
_FILTERED_LOGGERS = ("asyncio", "kasa", "kasa.discover")


def _install_camera_filter() -> None:
    for name in _FILTERED_LOGGERS:
        logging.getLogger(name).addFilter(_CAMERA_FILTER)


def _remove_camera_filter() -> None:
    for name in _FILTERED_LOGGERS:
        logging.getLogger(name).removeFilter(_CAMERA_FILTER)


def create_async_tplink_clientsession(hass: HomeAssistant) -> ClientSession:
    """Return aiohttp ClientSession configured for python-kasa."""
    return async_create_clientsession(hass, verify_ssl=False, cookie_jar=get_cookie_jar())


STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN


def _credentials_store(hass: HomeAssistant) -> Store:
    """Return the on-disk store used for shared cloud credentials."""
    return Store(hass, STORAGE_VERSION, STORAGE_KEY)


async def get_credentials(hass: HomeAssistant) -> Credentials | None:
    """Retrieve shared cloud credentials.

    FIX 7a: credentials used to live only in hass.data, which is RAM and is
    wiped on every restart. After a restart get_credentials() returned None,
    and because credentials_hash was not passed either (see FIX 7b),
    python-kasa fell back to blank Credentials() and the KLAP handshake
    failed with "Device response did not match our challenge".
    They are now persisted to .storage and the in-memory copy is just a cache.
    """
    data = hass.data.get(DOMAIN) or {}
    auth = data.get(CONF_AUTHENTICATION)
    if not auth:
        stored = await _credentials_store(hass).async_load()
        if stored and stored.get(CONF_AUTHENTICATION):
            auth = stored[CONF_AUTHENTICATION]
            # warm the in-memory cache so later calls skip the disk read
            hass.data.setdefault(DOMAIN, {})[CONF_AUTHENTICATION] = auth
    if not auth:
        return None
    return Credentials(auth[CONF_USERNAME], auth[CONF_PASSWORD])


async def set_credentials(hass: HomeAssistant, username: str, password: str) -> None:
    """Store shared cloud credentials in hass.data and on disk."""
    auth = {
        CONF_USERNAME: username,
        CONF_PASSWORD: password,
    }
    hass.data.setdefault(DOMAIN, {})[CONF_AUTHENTICATION] = auth
    await _credentials_store(hass).async_save({CONF_AUTHENTICATION: auth})


def mac_alias(mac: str) -> str:
    """Convert a MAC address to a short address for UI."""
    return mac.replace(":", "")[-4:].upper()


def legacy_device_id(device: Device) -> str:
    """Return legacy device id compatible with older HA entity ids."""
    device_id: str = device.device_id
    if "_" not in device_id:
        return device_id
    return device_id.split("_", 1)[1]


def get_device_name(device: Device, parent: Device | None = None) -> str | None:
    """Get a stable name for the device."""
    if device.alias:
        return device.alias
    if parent:
        siblings = [c for c in parent.children if c.device_type is device.device_type]
        if len(siblings) > 1:
            idx = [c.device_id for c in siblings].index(device.device_id) + 1
            return f"{device.device_type.value.capitalize()} {idx}"
        return f"{device.device_type.value.capitalize()}"
    return None


def _get_update_interval(entry: TPLinkConfigEntry, device: Device) -> timedelta:
    """Resolve the polling interval with this priority order:

    1. User-configured value saved in entry options  (highest priority)
    2. Per-model safe default from DEVICE_SCAN_INTERVALS
    3. Integration-wide DEFAULT_UPDATE_INTERVAL      (lowest priority)
    """
    if CONF_SCAN_INTERVAL in entry.options:
        seconds = int(entry.options[CONF_SCAN_INTERVAL])
        _LOGGER.debug(
            "Using user-configured scan interval of %ds for %s",
            seconds,
            device.host,
        )
        return timedelta(seconds=seconds)

    model_seconds = DEVICE_SCAN_INTERVALS.get(device.model)
    if model_seconds:
        _LOGGER.debug(
            "Using model default scan interval of %ds for %s (%s)",
            model_seconds,
            device.host,
            device.model,
        )
        return timedelta(seconds=model_seconds)

    return DEFAULT_UPDATE_INTERVAL


async def async_discover_devices(hass: HomeAssistant) -> dict[str, Device]:
    """Discover devices using python-kasa.

    NOTE: Discovery is optional and may log errors if TP-Link cameras respond.
    """
    creds = await get_credentials(hass)

    # Prefer limiting to configured adapters if possible.
    adapters = await network.async_get_adapters(hass)
    broadcast_addrs: list[str] = []
    for adapter in adapters:
        for ip in adapter.get("ipv4", []):
            bcast = ip.get("broadcast")
            if bcast:
                broadcast_addrs.append(bcast)

    _install_camera_filter()
    try:
        devices = await Discover.discover(
            credentials=creds,
            timeout=DISCOVERY_TIMEOUT,
            discovery_timeout=DISCOVERY_TIMEOUT,
            broadcast=broadcast_addrs or None,
        )
    except Exception as ex:  # noqa: BLE001
        _LOGGER.debug("Discovery failed: %s", ex, exc_info=True)
        return {}
    finally:
        _remove_camera_filter()

    # Return dict keyed by formatted MAC
    out: dict[str, Device] = {}
    for dev in devices.values():
        try:
            out[dev.mac.replace(":", "").upper()] = dev
        except Exception:  # noqa: BLE001
            continue
    return out


@callback
def _any_entry_enables_discovery(hass: HomeAssistant) -> bool:
    entries = hass.config_entries.async_entries(DOMAIN)
    return any(e.options.get(CONF_ENABLE_UDP_DISCOVERY, False) for e in entries)


@callback
def _update_discovery_scheduler(hass: HomeAssistant) -> None:
    data = hass.data.setdefault(DOMAIN, {})
    unsub = data.get(_DISCOVERY_UNSUB)

    enable = _any_entry_enables_discovery(hass)

    if enable and unsub is None:
        _LOGGER.warning(
            "TP-Link custom: UDP discovery ENABLED via Options. "
            "If TP-Link cameras respond, python-kasa may log discovery errors."
        )

        async def _tick(_now):
            # fire-and-forget to avoid blocking the scheduler
            hass.async_create_task(async_discover_devices(hass))

        data[_DISCOVERY_UNSUB] = async_track_time_interval(hass, _tick, DISCOVERY_INTERVAL)
        return

    if not enable and unsub is not None:
        _LOGGER.info("TP-Link custom: UDP discovery disabled via Options.")
        unsub()
        data.pop(_DISCOVERY_UNSUB, None)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up integration."""
    hass.data.setdefault(DOMAIN, {})
    # No auto-discovery at startup; controlled by Options.
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TPLinkConfigEntry) -> bool:
    """Set up a device from a config entry."""
    host: str = entry.data[CONF_HOST]
    port: int | None = entry.data.get(CONF_PORT)
    entry_use_http: bool = entry.data.get(CONF_USES_HTTP, False)
    entry_aes_keys = entry.data.get(CONF_AES_KEYS)
    entry_connection_params = entry.data.get(CONF_CONNECTION_PARAMETERS)
    entry_credentials_hash = entry.data.get(CONF_CREDENTIALS_HASH)

    creds = await get_credentials(hass)

    # FIX 7b + precedence: prefer live credentials; fall back to the saved
    # credentials_hash. Matches upstream — set one OR the other, not both.
    cfg_kwargs: dict[str, Any] = dict(
        port_override=port,
        timeout=CONNECT_TIMEOUT,
        aes_keys=entry_aes_keys,
    )
    # FIX 9: Shrink the multi-request batch for SMART/KLAP devices.
    # By default python-kasa packs every module (Time, AutoOff, Cloud,
    # DeviceModule, Matter) into ONE multipleRequest. On SMART devices such as
    # the S505 that single oversized encrypted request times out whenever the
    # device is briefly busy (overnight cloud check-in / firmware poll), taking
    # all the device's entities down together. Sending the modules in smaller
    # batches lets the device answer requests it can actually service under
    # load. python-kasa already falls back to batch_size=1 after a batch error
    # (smartprotocol.py), so this just pre-applies that fallback. Only SMART
    # connections are affected; IOT devices (ES20M, HS-series) ignore batching.
    if entry_connection_params:
        try:
            _conn = DeviceConnectionParameters.from_dict(entry_connection_params)
            if _conn.device_family.value.startswith("SMART"):
                cfg_kwargs["batch_size"] = SMART_BATCH_SIZE
        except Exception:  # noqa: BLE001
            pass
    if creds:
        cfg_kwargs["credentials"] = creds
    elif entry_credentials_hash:
        cfg_kwargs["credentials_hash"] = entry_credentials_hash
    if entry_connection_params:
        # FIX 6: Restore the protocol/transport detected at pairing time
        # (saved as CONF_CONNECTION_PARAMETERS). Without this, DeviceConfig
        # falls back to guessing the transport on every setup/reload, which
        # can pick the legacy XOR transport on port 9999 for devices that
        # actually speak a newer protocol (SSL/KLAP) — causing an immediate
        # "[Errno 111] Connect call failed" on 9999 instead of a real retry.
        try:
            cfg_kwargs["connection_type"] = DeviceConnectionParameters.from_dict(
                entry_connection_params
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Could not restore saved connection_type for %s; falling back "
                "to auto-detected defaults",
                host,
            )
    cfg = DeviceConfig(host, **cfg_kwargs)
    if entry_use_http:
        cfg.http_client = create_async_tplink_clientsession(hass)

    try:
        # FIX 5a: Wrap the initial connect+update with a hard timeout.
        # Without this, a slow or hung ES20M can block setup indefinitely.
        async with asyncio.timeout(CONNECT_TIMEOUT * 3):
            device = await Device.connect(config=cfg)
            await device.update()
    except TimeoutError as ex:
        raise ConfigEntryNotReady(
            f"Timed out connecting to {host} during setup"
        ) from ex
    except AuthenticationError as ex:
        # FIX 8b (self-healing): if we authenticated with a stored
        # credentials_hash and it failed (e.g. the TP-Link password changed),
        # drop the stale hash so the reauth flow is forced to use fresh
        # credentials instead of retrying a bad hash forever.
        if not creds and entry_credentials_hash:
            new_data = {
                k: v for k, v in entry.data.items() if k != CONF_CREDENTIALS_HASH
            }
            hass.config_entries.async_update_entry(entry, data=new_data)
        raise ConfigEntryAuthFailed from ex
    except KasaException as ex:
        raise ConfigEntryNotReady from ex

    # FIX 5b: Close the protocol connection opened during setup before the
    # coordinator starts polling. The ES20M has a very limited TCP stack —
    # leaving this connection open alongside the coordinator's first poll
    # causes two simultaneous connections, which triggers ECONNRESET (Errno 104).
    try:
        await device.protocol.close()
    except Exception:  # noqa: BLE001
        pass

    # Resolve polling interval: options → model default → global default
    update_interval = _get_update_interval(entry, device)
    _LOGGER.info(
        "TP-Link %s (%s) polling interval: %ds",
        device.model,
        host,
        update_interval.total_seconds(),
    )

    coordinator = TPLinkDataUpdateCoordinator(hass, device, update_interval, entry)
    await coordinator.async_config_entry_first_refresh()

    # Runtime data
    camera_creds = None
    if (cam := entry.data.get("camera_credentials")) and isinstance(cam, dict):
        if cam.get(CONF_USERNAME) and cam.get(CONF_PASSWORD):
            camera_creds = Credentials(cam[CONF_USERNAME], cam[CONF_PASSWORD])

    entry.runtime_data = TPLinkData(
        parent_coordinator=coordinator,
        camera_credentials=camera_creds,
        live_view=entry.data.get("live_view"),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Keep discovery scheduler in sync with options
#    _update_discovery_scheduler(hass)
#    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    return True


async def _async_entry_updated(hass: HomeAssistant, entry: TPLinkConfigEntry) -> None:
    """Handle updates to entry options changes."""
    _update_discovery_scheduler(hass)


async def async_unload_entry(hass: HomeAssistant, entry: TPLinkConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and entry.runtime_data:
        try:
            await entry.runtime_data.parent_coordinator.async_shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            await entry.runtime_data.parent_coordinator.device.protocol.close()
        except Exception:  # noqa: BLE001
            pass

    # Re-evaluate scheduler (maybe last entry removed)
    _update_discovery_scheduler(hass)
    return unload_ok