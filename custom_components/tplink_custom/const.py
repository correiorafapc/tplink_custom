"""Const for TP-Link."""

from __future__ import annotations

from typing import Final

from kasa.smart.modules.clean import AreaUnit

from homeassistant.const import Platform, UnitOfArea, UnitOfTemperature

DOMAIN = "tplink_custom"

DISCOVERY_TIMEOUT = 5  # Home Assistant will complain if startup takes > 10s
CONNECT_TIMEOUT = 5

# FIX 9: multipleRequest batch size for SMART/KLAP devices (e.g. S505).
# The python-kasa default is 5, which packs every module into one oversized
# encrypted request that times out when the device is briefly busy. 2 keeps
# requests small enough to service under load while still batching a little.
# Set to 1 to disable batching entirely if timeouts persist.
SMART_BATCH_SIZE = 2
DEFAULT_SCAN_INTERVAL = 5  # Every device polls at 5s unless overridden per-device in the UI

# Identifier used for primary control state.
PRIMARY_STATE_ID = "state"

ATTR_CURRENT_A: Final = "current_a"
ATTR_CURRENT_POWER_W: Final = "current_power_w"
ATTR_TODAY_ENERGY_KWH: Final = "today_energy_kwh"
ATTR_TOTAL_ENERGY_KWH: Final = "total_energy_kwh"

CONF_DEVICE_CONFIG: Final = "device_config"
CONF_CREDENTIALS_HASH: Final = "credentials_hash"
CONF_CONNECTION_PARAMETERS: Final = "connection_parameters"
CONF_USES_HTTP: Final = "uses_http"
CONF_AES_KEYS: Final = "aes_keys"
CONF_CAMERA_CREDENTIALS = "camera_credentials"
CONF_LIVE_VIEW = "live_view"
CONF_ENABLE_UDP_DISCOVERY: Final = "enable_udp_discovery"

CONF_CONFIG_ENTRY_MINOR_VERSION: Final = 5

PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.CLIMATE,
    Platform.FAN,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SIREN,
    Platform.SWITCH,
    Platform.VACUUM,
]

UNIT_MAPPING = {
    "celsius": UnitOfTemperature.CELSIUS,
    "fahrenheit": UnitOfTemperature.FAHRENHEIT,
    AreaUnit.Sqm: UnitOfArea.SQUARE_METERS,
    AreaUnit.Sqft: UnitOfArea.SQUARE_FEET,
}