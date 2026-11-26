"""Standalone offline BLE client for CUKTECH 10 GaN Charger Ultra (njcuk.fitting.ad1204)."""

from .ble_client import BleakTransport, CuktechUltraClient, DiscoveredCharger, scan_chargers
from .config import DeviceConfig, load_config, save_config
from .device import (
    ChargerState,
    ExtendedProtocolSwitches,
    LegacyProtocolSwitches,
    PortTelemetry,
    SceneMode,
    ScreenLanguage,
    ScreenTimeout,
)
from .mock_device import MOCK_DEFAULT_TOKEN, MockChargerTransport

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "BleakTransport",
    "ChargerState",
    "CuktechUltraClient",
    "DeviceConfig",
    "DiscoveredCharger",
    "ExtendedProtocolSwitches",
    "LegacyProtocolSwitches",
    "MOCK_DEFAULT_TOKEN",
    "MockChargerTransport",
    "PortTelemetry",
    "SceneMode",
    "ScreenLanguage",
    "ScreenTimeout",
    "load_config",
    "save_config",
    "scan_chargers",
]
