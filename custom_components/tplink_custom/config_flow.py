"""Config flow for TP-Link Smart Home (Custom)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kasa import AuthenticationError, Credentials, Device, Discover, KasaException
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from . import create_async_tplink_clientsession, get_credentials, mac_alias, set_credentials
from .const import (
    CONF_AES_KEYS,
    CONF_CONNECTION_PARAMETERS,
    CONF_CREDENTIALS_HASH,
    CONF_ENABLE_UDP_DISCOVERY,
    CONF_USES_HTTP,
    CONNECT_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema({vol.Required(CONF_HOST): str})
STEP_AUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

# Polling interval boundaries (seconds)
MIN_SCAN_INTERVAL = 1
MAX_SCAN_INTERVAL = 600

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENABLE_UDP_DISCOVERY, default=False): bool,
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int),
            vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
        ),
    }
)


class TPLinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the TP-Link custom integration."""

    VERSION = 1
    # Keep minor version in const.py for migrations if you implement them later.
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int | None = None
        self._device: Device | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        """Create the options flow."""
        return TPLinkOptionsFlow()

    @staticmethod
    def _parse_host_port(host_str: str) -> tuple[str, int | None]:
        """Parse host[:port] and return (host, port)."""
        host, sep, port_str = host_str.rpartition(":")
        if sep and host and port_str.isdigit():
            return host, int(port_str)
        return host_str, None

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle discovery via DHCP.

        FIX 9: the manifest declares DHCP matchers (including
        ``registered_devices: true``) but the fork had no handler, so HA
        raised an error on every matching lease and — more importantly — the
        auto-IP-recovery never worked. This handler matches the device by MAC
        to an existing entry and updates its stored host if the IP changed,
        which fixes the "device moved to a new IP, setup now fails" problem
        without introducing any interactive discovery UI.
        """
        mac = dr.format_mac(discovery_info.macaddress)
        host = discovery_info.ip

        await self.async_set_unique_id(mac, raise_on_progress=False)
        # If this MAC already has an entry, update its host (if changed) and
        # abort. This is the auto-heal path for DHCP-reassigned addresses.
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        # Not currently configured. Stash the host and hand off to the normal
        # user step so adding it is a one-click confirm rather than silent
        # auto-add (keeps the fork's "no surprise devices" behaviour).
        self._host = host
        self._port = None
        return await self.async_step_user_discovery_confirm()

    async def async_step_user_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a DHCP-discovered device before connecting."""
        assert self._host is not None
        if user_input is not None:
            try:
                device = await self._try_connect(self._host, self._port)
            except AuthenticationError:
                return await self.async_step_user_auth_confirm()
            except KasaException:
                return self.async_abort(reason="cannot_connect")
            if device is None:
                return self.async_abort(reason="cannot_connect")
            return await self._create_entry_from_device(device)

        placeholders = {"host": self._host}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="user_discovery_confirm",
            description_placeholders=placeholders,
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_host = user_input[CONF_HOST].strip()
            host, port = self._parse_host_port(raw_host)
            self._host = host
            self._port = port

            try:
                device = await self._try_connect(host, port)
            except AuthenticationError:
                return await self.async_step_user_auth_confirm()
            except KasaException:
                errors["base"] = "cannot_connect"
            else:
                if device is None:
                    errors["base"] = "cannot_connect"
                else:
                    return await self._create_entry_from_device(device)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_user_auth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for TP-Link cloud credentials when needed."""
        errors: dict[str, str] = {}

        assert self._host is not None

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            creds = Credentials(username, password)
            try:
                device = await self._try_connect(self._host, self._port, creds)
            except AuthenticationError:
                errors[CONF_PASSWORD] = "invalid_auth"
            except KasaException:
                errors["base"] = "cannot_connect"
            else:
                if device is None:
                    errors["base"] = "cannot_connect"
                else:
                    await set_credentials(self.hass, username, password)
                    return await self._create_entry_from_device(device)

        placeholders = {"host": self._host}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="user_auth_confirm",
            data_schema=STEP_AUTH_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth when a device's stored credentials stop working.

        FIX 8: the fork raised ConfigEntryAuthFailed on auth failure but had
        no reauth handler, so HA could not recover — the entry just stayed
        failed. This mirrors upstream: prompt for fresh credentials, verify
        them against the device, then persist and reload.
        """
        reauth_entry = self._get_reauth_entry()
        self._host = reauth_entry.data[CONF_HOST]
        self._port = reauth_entry.data.get(CONF_PORT)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that asks for updated credentials during reauth."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        assert self._host is not None

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            creds = Credentials(username, password)
            try:
                device = await self._try_connect(self._host, self._port, creds)
            except AuthenticationError as ex:
                errors[CONF_PASSWORD] = "invalid_auth"
                placeholders["error"] = str(ex)
            except KasaException as ex:
                errors["base"] = "cannot_connect"
                placeholders["error"] = str(ex)
            else:
                if device is None:
                    errors["base"] = "cannot_connect"
                else:
                    # Persist fresh credentials (RAM + disk) and refresh the
                    # entry's credentials_hash from the reconnected device.
                    await set_credentials(self.hass, username, password)
                    reauth_entry = self._get_reauth_entry()
                    updates = dict(reauth_entry.data)
                    if device.credentials_hash:
                        updates[CONF_CREDENTIALS_HASH] = device.credentials_hash
                    return self.async_update_reload_and_abort(
                        reauth_entry, data=updates
                    )

        placeholders["host"] = self._host
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_AUTH_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _try_connect(
        self,
        host: str,
        port: int | None,
        creds: Credentials | None = None,
    ) -> Device | None:
        """Try to discover and update a device."""
        if creds is None:
            creds = await get_credentials(self.hass)

        # Use Discover.discover_single first (fast path)
        device: Device | None = None
        try:
            device = await Discover.discover_single(host, credentials=creds, port=port)
        except AuthenticationError:
            raise
        except Exception:  # noqa: BLE001
            device = None

        # Fallback: speculative connect (legacy)
        if device is None:
            try:
                device = await Device.connect(
                    config=Device.Config(host, port_override=port)  # type: ignore[attr-defined]
                )
            except Exception:  # noqa: BLE001
                return None

        # Ensure HTTP client if needed
        if getattr(device.config, "uses_http", False):
            device.config.http_client = create_async_tplink_clientsession(self.hass)
        device.config.timeout = CONNECT_TIMEOUT

        await device.update()
        self._device = device

        # Unique id = MAC
        await self.async_set_unique_id(dr.format_mac(device.mac), raise_on_progress=False)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        return device

    async def _create_entry_from_device(self, device: Device) -> ConfigFlowResult:
        """Create config entry from a connected device."""
        data: dict[str, Any] = {
            CONF_HOST: device.host,
            CONF_CONNECTION_PARAMETERS: device.config.connection_type.to_dict(),
            CONF_USES_HTTP: device.config.uses_http,
        }
        if device.config.aes_keys:
            data[CONF_AES_KEYS] = device.config.aes_keys
        if device.credentials_hash:
            data[CONF_CREDENTIALS_HASH] = device.credentials_hash
        if device.config.port_override:
            data[CONF_PORT] = device.config.port_override

        title = device.alias or mac_alias(device.mac)
        return self.async_create_entry(title=title, data=data)


class TPLinkOptionsFlow(OptionsFlowWithReload):
    """Options flow handler for TP-Link custom."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                # Merge saved options with defaults so the form always
                # shows the current value (or the default on first open).
                vol.Schema(
                    {
                        vol.Required(
                            CONF_ENABLE_UDP_DISCOVERY,
                            default=self.config_entry.options.get(
                                CONF_ENABLE_UDP_DISCOVERY, False
                            ),
                        ): bool,
                        vol.Required(
                            CONF_SCAN_INTERVAL,
                            default=self.config_entry.options.get(
                                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                            ),
                        ): NumberSelector(
                            NumberSelectorConfig(
                                min=MIN_SCAN_INTERVAL,
                                max=MAX_SCAN_INTERVAL,
                                step=5,
                                unit_of_measurement="seconds",
                                mode=NumberSelectorMode.BOX,
                            )
                        ),
                    }
                ),
                self.config_entry.options,
            ),
        )