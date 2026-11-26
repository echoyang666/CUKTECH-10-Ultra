"""CUKTECH 10 GaN Charger Ultra (njcuk.fitting.ad1204) MIoT-Spec device model and bitfield codecs."""

from __future__ import annotations

import struct
import time
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any

from .spec import PropertyReadReq, PropertyValue, PropertyWriteReq, SpecValueType

# Target device identifiers
TARGET_MODELS = ("njcuk.fitting.ad1204", "njcuk.fitting.1204e")
TARGET_PDID = 26126  # 0x660E — verified live from CUKTECH 10 Ultra 0xFE95 MiBeacon advertisement

# MiBLE GATT UUIDs (Service 0xFE95)
UUID_SERVICE_FE95 = "0000fe95-0000-1000-8000-00805f9b34fb"
UUID_CHAR_FIRMWARE = "00000004-0000-1000-8000-00805f9b34fb"
UUID_CHAR_AUTH_CTRL = "00000010-0000-1000-8000-00805f9b34fb"
UUID_CHAR_AUTH_DATA = "00000019-0000-1000-8000-00805f9b34fb"
UUID_CHAR_SPEC_CMD = "0000001a-0000-1000-8000-00805f9b34fb"
UUID_CHAR_SPEC_NOTIFY = "0000001b-0000-1000-8000-00805f9b34fb"

# Service IDs
SIID_DEVICE_INFO = 1
SIID_CHARGER = 2

# Service 1 Property IDs
PIID_MANUFACTURER = 1
PIID_MODEL = 2
PIID_SERIAL_NUMBER = 3
PIID_FIRMWARE_REVISION = 4

# Service 2 Property IDs
PIID_PORT_INFO_C1 = 1
PIID_PORT_INFO_C2 = 2
PIID_PORT_INFO_C3 = 3
PIID_PORT_INFO_A = 4
PIID_SCENE_MODE = 5
PIID_SCREEN_SAVE_TIME = 6
PIID_PROTOCOL_CTRL = 7
PIID_DELAY_DOWN_SETTING = 8
PIID_DELAY_TIME_C1 = 9
PIID_DELAY_TIME_C2 = 10
PIID_DELAY_TIME_C3 = 11
PIID_DELAY_TIME_A = 12
PIID_SCREEN_LANGUAGE = 13
PIID_ENTER = 14
PIID_USB_A_ALWAYS_ON = 15
PIID_PORT_CTRL = 16
PIID_C1_C2_PROTOCOL = 17
PIID_C3_A_PROTOCOL = 18
PIID_SCREENOFF_WHILE_IDLE = 19
PIID_SCREEN_DIRECTION_LOCK = 20
PIID_PROTOCOL_CTL_EXTEND = 21

# Service 2 Action IDs
AIID_RESET_SETTING = 1

PORT_NAMES = ("c1", "c2", "c3", "a")
PORT_BIT_INDEX = {"c1": 0, "c2": 1, "c3": 2, "a": 3}
PORT_INFO_PIID = {
    "c1": PIID_PORT_INFO_C1,
    "c2": PIID_PORT_INFO_C2,
    "c3": PIID_PORT_INFO_C3,
    "a": PIID_PORT_INFO_A,
}
PORT_DELAY_PIID = {
    "c1": PIID_DELAY_TIME_C1,
    "c2": PIID_DELAY_TIME_C2,
    "c3": PIID_DELAY_TIME_C3,
    "a": PIID_DELAY_TIME_A,
}


class SceneMode(IntEnum):
    AI_SMART = 1        # AI 智能功率分配
    DIGITAL_ECO = 2     # 数码生态（多设备均衡快充）
    TURBO_SINGLE = 3    # 极速单充
    BALANCED = 4        # 均衡输出

    @property
    def label(self) -> str:
        return {
            SceneMode.AI_SMART: "AI智能",
            SceneMode.DIGITAL_ECO: "数码生态",
            SceneMode.TURBO_SINGLE: "极速单充",
            SceneMode.BALANCED: "均衡输出",
        }[self]


class ScreenTimeout(IntEnum):
    MIN_1 = 5
    MIN_5 = 1
    MIN_10 = 2
    MIN_30 = 3
    ALWAYS_ON = 4

    @property
    def label(self) -> str:
        return {
            ScreenTimeout.MIN_1: "1分钟",
            ScreenTimeout.MIN_5: "5分钟",
            ScreenTimeout.MIN_10: "10分钟",
            ScreenTimeout.MIN_30: "30分钟",
            ScreenTimeout.ALWAYS_ON: "常亮",
        }[self]

    @classmethod
    def from_name(cls, text: str) -> ScreenTimeout:
        key = text.strip().lower()
        mapping = {
            "1m": cls.MIN_1,
            "1": cls.MIN_1,
            "5m": cls.MIN_5,
            "5": cls.MIN_5,
            "10m": cls.MIN_10,
            "10": cls.MIN_10,
            "30m": cls.MIN_30,
            "30": cls.MIN_30,
            "always": cls.ALWAYS_ON,
            "on": cls.ALWAYS_ON,
            "always_on": cls.ALWAYS_ON,
        }
        if key not in mapping:
            raise ValueError(f"Unknown screen timeout '{text}'. Use: 1m, 5m, 10m, 30m, always")
        return mapping[key]


class ScreenLanguage(IntEnum):
    ENGLISH = 0
    CHINESE = 1

    @property
    def label(self) -> str:
        return "中文" if self == ScreenLanguage.CHINESE else "English"


PROTOCOL_ID_NAMES: dict[int, str] = {
    0: "NONE",
    1: "5V",
    2: "5V",
    3: "QC",
    4: "AFC",
    5: "FCP",
    6: "SCP",
    7: "PD",
    8: "PPS",
    9: "PPS",
    10: "UFCS",
}


def protocol_id_to_str(protocol_id: int) -> str:
    return PROTOCOL_ID_NAMES.get(protocol_id, f"UNKNOWN({protocol_id})")


def supports_spec_v2(firmware_version: str | None) -> bool:
    """Check whether firmware version >= 2.1.2_0040 (enables piid 19..21)."""
    if not firmware_version:
        return True
    try:
        main_part, *rest = firmware_version.strip().split("_")
        nums = tuple(int(x) for x in main_part.split("."))
        build = int(rest[0]) if rest else 0
        return (nums, build) >= ((2, 1, 2), 40)
    except ValueError:
        return True


@dataclass
class PortTelemetry:
    port: str
    voltage_v: float = 0.0
    current_a: float = 0.0
    power_w: float = 0.0
    protocol_raw: int = 0
    status: int = 0
    shared_c3_a: bool = False
    protocol_name: str = "NONE"
    protocol_power_w: int = 0
    enabled: bool = True
    delay_enabled: bool = False
    delay_minutes: int = 0

    @property
    def active(self) -> bool:
        return self.status in (1, 0x11) or self.power_w > 0.0


@dataclass
class LegacyProtocolSwitches:
    scp: bool = True
    mipps: bool = True
    ufcs: bool = True


@dataclass
class ExtendedProtocolSwitches:
    c1_pd: bool = True
    c1_pps: bool = True
    c1_ufcs: bool = True
    c2_pd: bool = True
    c2_pps: bool = True
    c2_ufcs: bool = True
    c3_scp: bool = True
    c3_ufcs: bool = True
    a_scp: bool = True
    a_ufcs: bool = True


@dataclass
class ChargerState:
    manufacturer: str = "CUKTECH"
    model: str = "njcuk.fitting.ad1204"
    serial_number: str = ""
    firmware_revision: str = ""
    connected: bool = False
    mock_mode: bool = False
    last_updated: float = 0.0

    ports: dict[str, PortTelemetry] = field(
        default_factory=lambda: {name: PortTelemetry(port=name) for name in PORT_NAMES}
    )
    scene_mode: int = int(SceneMode.AI_SMART)
    screen_save_time: int = int(ScreenTimeout.MIN_5)
    screen_language: int = int(ScreenLanguage.CHINESE)
    usb_a_always_on: bool = False
    screenoff_while_idle: bool = False
    screen_direction_lock: bool = False
    protocol_v1: LegacyProtocolSwitches = field(default_factory=LegacyProtocolSwitches)
    protocol_v2: ExtendedProtocolSwitches = field(default_factory=ExtendedProtocolSwitches)
    spec_v2_supported: bool = True

    @property
    def total_power_w(self) -> float:
        return round(sum(p.power_w for p in self.ports.values()), 1)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["total_power_w"] = self.total_power_w
        try:
            data["scene_mode_label"] = SceneMode(self.scene_mode).label
        except ValueError:
            data["scene_mode_label"] = f"Mode {self.scene_mode}"
        try:
            data["screen_save_time_label"] = ScreenTimeout(self.screen_save_time).label
        except ValueError:
            data["screen_save_time_label"] = f"Code {self.screen_save_time}"
        try:
            data["screen_language_label"] = ScreenLanguage(self.screen_language).label
        except ValueError:
            data["screen_language_label"] = str(self.screen_language)
        return data

    def apply_property(self, prop: PropertyValue) -> None:
        """Apply a decoded MIoT-BLE Spec property read/notification onto this state."""
        if prop.code != 0 or prop.value is None:
            return
        self.last_updated = time.time()

        if prop.siid == SIID_DEVICE_INFO:
            if prop.piid == PIID_MANUFACTURER:
                self.manufacturer = str(prop.value)
            elif prop.piid == PIID_MODEL:
                self.model = str(prop.value)
            elif prop.piid == PIID_SERIAL_NUMBER:
                self.serial_number = str(prop.value)
            elif prop.piid == PIID_FIRMWARE_REVISION:
                self.firmware_revision = str(prop.value)
                self.spec_v2_supported = supports_spec_v2(self.firmware_revision)
            return

        if prop.siid != SIID_CHARGER:
            return

        val = prop.value
        if prop.piid in (PIID_PORT_INFO_C1, PIID_PORT_INFO_C2, PIID_PORT_INFO_C3, PIID_PORT_INFO_A):
            port_name = PORT_NAMES[prop.piid - 1]
            decoded = decode_port_info(int(val), port_name)
            p = self.ports[port_name]
            p.voltage_v = decoded["voltage_v"]
            p.current_a = decoded["current_a"]
            p.power_w = decoded["power_w"]
            p.protocol_raw = decoded["protocol_raw"]
            p.status = decoded["status"]
            p.shared_c3_a = decoded["shared_c3_a"]
        elif prop.piid == PIID_SCENE_MODE:
            self.scene_mode = int(val)
        elif prop.piid == PIID_SCREEN_SAVE_TIME:
            self.screen_save_time = int(val)
        elif prop.piid == PIID_PROTOCOL_CTRL:
            self.protocol_v1 = decode_protocol_ctrl_v1(int(val))
        elif prop.piid == PIID_DELAY_DOWN_SETTING:
            mask = decode_port_mask(int(val))
            for name, flag in mask.items():
                self.ports[name].delay_enabled = flag
        elif prop.piid in (PIID_DELAY_TIME_C1, PIID_DELAY_TIME_C2, PIID_DELAY_TIME_C3, PIID_DELAY_TIME_A):
            port_name = PORT_NAMES[prop.piid - PIID_DELAY_TIME_C1]
            self.ports[port_name].delay_minutes = int(val)
        elif prop.piid == PIID_SCREEN_LANGUAGE:
            self.screen_language = int(val)
        elif prop.piid == PIID_USB_A_ALWAYS_ON:
            self.usb_a_always_on = bool(val)
        elif prop.piid == PIID_PORT_CTRL:
            mask = decode_port_mask(int(val))
            for name, flag in mask.items():
                self.ports[name].enabled = flag
        elif prop.piid == PIID_C1_C2_PROTOCOL:
            c1, c2 = decode_dual_protocol(int(val))
            self.ports["c1"].protocol_name, self.ports["c1"].protocol_power_w = c1
            self.ports["c2"].protocol_name, self.ports["c2"].protocol_power_w = c2
        elif prop.piid == PIID_C3_A_PROTOCOL:
            c3, a = decode_dual_protocol(int(val))
            self.ports["c3"].protocol_name, self.ports["c3"].protocol_power_w = c3
            self.ports["a"].protocol_name, self.ports["a"].protocol_power_w = a
        elif prop.piid == PIID_SCREENOFF_WHILE_IDLE:
            self.screenoff_while_idle = bool(int(val))
        elif prop.piid == PIID_SCREEN_DIRECTION_LOCK:
            self.screen_direction_lock = bool(int(val))
        elif prop.piid == PIID_PROTOCOL_CTL_EXTEND:
            self.protocol_v2 = decode_protocol_ctl_extend(int(val))


# ---------------------------------------------------------------------------
# Bitfield codecs
# ---------------------------------------------------------------------------


def decode_port_info(raw: int, port: str = "c1") -> dict[str, Any]:
    """Decode uint32 port_info_{c1,c2,c3,a} (piid 1..4)."""
    voltage_v = round(((raw >> 24) & 0xFF) / 10.0, 1)
    current_a = round(((raw >> 16) & 0xFF) / 10.0, 1)
    protocol_raw = (raw >> 8) & 0xFF
    status = raw & 0xFF
    power_w = round(voltage_v * current_a, 1)
    shared_c3_a = port == "c3" and status == 0x11
    return {
        "voltage_v": voltage_v,
        "current_a": current_a,
        "power_w": power_w,
        "protocol_raw": protocol_raw,
        "status": status,
        "shared_c3_a": shared_c3_a,
    }


def encode_port_info(voltage_v: float, current_a: float, protocol_raw: int, status: int) -> int:
    """Inverse of decode_port_info (useful for tests and mock device)."""
    v = max(0, min(255, round(voltage_v * 10)))
    i = max(0, min(255, round(current_a * 10)))
    return ((v & 0xFF) << 24) | ((i & 0xFF) << 16) | ((protocol_raw & 0xFF) << 8) | (status & 0xFF)


def decode_dual_protocol(raw: int) -> tuple[tuple[str, int], tuple[str, int]]:
    """Decode uint32 c1_c2_protocol (2.17) or c3_a_protocol (2.18).

    Returns ((first_proto_str, first_power_w), (second_proto_str, second_power_w)).
    """
    p1_id = (raw >> 24) & 0xFF
    p1_pwr = (raw >> 16) & 0xFF
    p2_id = (raw >> 8) & 0xFF
    p2_pwr = raw & 0xFF
    return (protocol_id_to_str(p1_id), p1_pwr), (protocol_id_to_str(p2_id), p2_pwr)


def encode_dual_protocol(p1_id: int, p1_pwr: int, p2_id: int, p2_pwr: int) -> int:
    return ((p1_id & 0xFF) << 24) | ((p1_pwr & 0xFF) << 16) | ((p2_id & 0xFF) << 8) | (p2_pwr & 0xFF)


def decode_port_mask(raw: int) -> dict[str, bool]:
    """Decode 4-bit port bitmask used by port_ctrl (2.16) and delay_down_setting (2.8)."""
    return {
        "c1": bool(raw & 0x01),
        "c2": bool(raw & 0x02),
        "c3": bool(raw & 0x04),
        "a": bool(raw & 0x08),
    }


def encode_port_mask(c1: bool, c2: bool, c3: bool, a: bool) -> int:
    return (1 if c1 else 0) | ((1 if c2 else 0) << 1) | ((1 if c3 else 0) << 2) | ((1 if a else 0) << 3)


def decode_protocol_ctrl_v1(raw: int) -> LegacyProtocolSwitches:
    """Decode legacy Spec v1 protocol_ctrl (2.7): bit0=SCP, bit1=MiPPS, bit2=UFCS."""
    return LegacyProtocolSwitches(
        scp=bool(raw & 0x01),
        mipps=bool(raw & 0x02),
        ufcs=bool(raw & 0x04),
    )


def encode_protocol_ctrl_v1(switches: LegacyProtocolSwitches) -> int:
    return (1 if switches.scp else 0) | ((1 if switches.mipps else 0) << 1) | ((1 if switches.ufcs else 0) << 2)


def decode_protocol_ctl_extend(raw: int) -> ExtendedProtocolSwitches:
    """Decode Spec v2 per-port protocol_ctl_extend (2.21, uint32)."""
    c1 = raw & 0xFF
    c2 = (raw >> 8) & 0xFF
    c3 = (raw >> 16) & 0xFF
    a = (raw >> 24) & 0xFF
    return ExtendedProtocolSwitches(
        c1_pd=bool(c1 & 0x01),
        c1_pps=bool(c1 & 0x02),
        c1_ufcs=bool(c1 & 0x04),
        c2_pd=bool(c2 & 0x01),
        c2_pps=bool(c2 & 0x02),
        c2_ufcs=bool(c2 & 0x04),
        c3_ufcs=bool(c3 & 0x01),
        c3_scp=bool(c3 & 0x02),
        a_ufcs=bool(a & 0x01),
        a_scp=bool(a & 0x02),
    )


def encode_protocol_ctl_extend(s: ExtendedProtocolSwitches) -> int:
    """Encode Spec v2 per-port protocol_ctl_extend (2.21, uint32)."""
    c1 = 0x08 | ((1 if s.c1_ufcs else 0) << 2) | ((1 if s.c1_pps else 0) << 1) | (1 if s.c1_pd else 0)
    c2 = 0x08 | ((1 if s.c2_ufcs else 0) << 2) | ((1 if s.c2_pps else 0) << 1) | (1 if s.c2_pd else 0)
    c3 = ((1 if s.c3_scp else 0) << 1) | (1 if s.c3_ufcs else 0)
    a = ((1 if s.a_scp else 0) << 1) | (1 if s.a_ufcs else 0)
    return (a << 24) | (c3 << 16) | (c2 << 8) | c1


# ---------------------------------------------------------------------------
# Standard Property Batches
# ---------------------------------------------------------------------------


def build_device_info_reads() -> list[PropertyReadReq]:
    return [
        PropertyReadReq(SIID_DEVICE_INFO, PIID_MANUFACTURER),
        PropertyReadReq(SIID_DEVICE_INFO, PIID_MODEL),
        PropertyReadReq(SIID_DEVICE_INFO, PIID_SERIAL_NUMBER),
        PropertyReadReq(SIID_DEVICE_INFO, PIID_FIRMWARE_REVISION),
    ]


def build_telemetry_reads() -> list[PropertyReadReq]:
    return [
        PropertyReadReq(SIID_CHARGER, PIID_PORT_INFO_C1),
        PropertyReadReq(SIID_CHARGER, PIID_PORT_INFO_C2),
        PropertyReadReq(SIID_CHARGER, PIID_PORT_INFO_C3),
        PropertyReadReq(SIID_CHARGER, PIID_PORT_INFO_A),
        PropertyReadReq(SIID_CHARGER, PIID_C1_C2_PROTOCOL),
        PropertyReadReq(SIID_CHARGER, PIID_C3_A_PROTOCOL),
    ]


def build_settings_reads(include_v2: bool = True) -> list[PropertyReadReq]:
    reqs = [
        PropertyReadReq(SIID_CHARGER, PIID_SCENE_MODE),
        PropertyReadReq(SIID_CHARGER, PIID_SCREEN_SAVE_TIME),
        PropertyReadReq(SIID_CHARGER, PIID_PROTOCOL_CTRL),
        PropertyReadReq(SIID_CHARGER, PIID_DELAY_DOWN_SETTING),
        PropertyReadReq(SIID_CHARGER, PIID_DELAY_TIME_C1),
        PropertyReadReq(SIID_CHARGER, PIID_DELAY_TIME_C2),
        PropertyReadReq(SIID_CHARGER, PIID_DELAY_TIME_C3),
        PropertyReadReq(SIID_CHARGER, PIID_DELAY_TIME_A),
        PropertyReadReq(SIID_CHARGER, PIID_SCREEN_LANGUAGE),
        PropertyReadReq(SIID_CHARGER, PIID_USB_A_ALWAYS_ON),
        PropertyReadReq(SIID_CHARGER, PIID_PORT_CTRL),
    ]
    if include_v2:
        reqs.extend(
            [
                PropertyReadReq(SIID_CHARGER, PIID_SCREENOFF_WHILE_IDLE),
                PropertyReadReq(SIID_CHARGER, PIID_SCREEN_DIRECTION_LOCK),
                PropertyReadReq(SIID_CHARGER, PIID_PROTOCOL_CTL_EXTEND),
            ]
        )
    return reqs


def build_enter_app_write() -> PropertyWriteReq:
    """Write enter=2 (2.14) to notify charger that a foreground client is active."""
    return PropertyWriteReq(SIID_CHARGER, PIID_ENTER, SpecValueType.UINT8, 2)


# ---------------------------------------------------------------------------
# MiBeacon 0xFE95 Advertisement Parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MiBeaconInfo:
    frame_control: int
    pdid: int
    frame_counter: int
    mac: str | None


def parse_mibeacon_fe95(service_data: bytes) -> MiBeaconInfo | None:
    """Parse Xiaomi MiBeacon 0xFE95 BLE advertisement Service Data."""
    if len(service_data) < 5:
        return None
    frame_ctrl, pdid, counter = struct.unpack_from("<HHB", service_data, 0)
    has_mac = bool(frame_ctrl & 0x0010)
    mac: str | None = None
    if has_mac and len(service_data) >= 11:
        mac_bytes = service_data[5:11][::-1]
        mac = ":".join(f"{b:02X}" for b in mac_bytes)
    return MiBeaconInfo(
        frame_control=frame_ctrl,
        pdid=pdid,
        frame_counter=counter,
        mac=mac,
    )
