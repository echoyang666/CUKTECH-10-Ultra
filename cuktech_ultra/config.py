"""Local configuration storage (~/.cuktech-ultra/config.json) for offline BLE operation."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CONFIG_DIR = Path.home() / ".cuktech-ultra"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"


@dataclass
class DeviceConfig:
    token: str = ""
    mac: str = ""
    ble_address: str = ""  # macOS CoreBluetooth UUID or Linux/Windows MAC
    name: str = "CUKTECH 10 Ultra"
    did: str = ""
    model: str = "njcuk.fitting.ad1204"

    @property
    def target_identifier(self) -> str:
        return self.ble_address or self.mac


def load_config(path: Path | None = None) -> DeviceConfig:
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        return DeviceConfig()
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    return DeviceConfig(
        token=str(data.get("token", "")),
        mac=str(data.get("mac", "")).upper(),
        ble_address=str(data.get("ble_address", "")),
        name=str(data.get("name", "CUKTECH 10 Ultra")),
        did=str(data.get("did", "")),
        model=str(data.get("model", "njcuk.fitting.ad1204")),
    )


def save_config(cfg: DeviceConfig, path: Path | None = None) -> Path:
    cfg_path = path or DEFAULT_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
    return cfg_path
