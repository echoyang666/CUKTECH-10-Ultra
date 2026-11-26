"""Full-fidelity virtual CUKTECH 10 Ultra BLE peripheral for offline testing and demo mode.

Speaks the exact same GATT 0xFE95 + MiBLE Parcel + StandardAuth + AES-128-CCM + MIoT-BLE Spec v2
protocol as real hardware, allowing end-to-end verification of the entire client stack.
"""

from __future__ import annotations

import asyncio
import math
import os
import struct
import time
from collections.abc import Callable
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from .ble_client import (
    AUTH_CMD_LOGIN_START,
    PARCEL_TYPE_APP_CONFIRM,
    PARCEL_TYPE_APP_RAND,
    PARCEL_TYPE_DEV_CONFIRM,
    PARCEL_TYPE_DEV_RAND,
    PARCEL_TYPE_SPEC,
)
from .crypto import (
    MIC_TAG_LENGTH,
    SessionKeys,
    compute_app_confirm,
    compute_dev_confirm,
    derive_session_keys,
    parse_token_hex,
)
from .device import (
    AIID_RESET_SETTING,
    PIID_C1_C2_PROTOCOL,
    PIID_C3_A_PROTOCOL,
    PIID_DELAY_DOWN_SETTING,
    PIID_DELAY_TIME_A,
    PIID_DELAY_TIME_C1,
    PIID_DELAY_TIME_C2,
    PIID_DELAY_TIME_C3,
    PIID_ENTER,
    PIID_FIRMWARE_REVISION,
    PIID_MANUFACTURER,
    PIID_MODEL,
    PIID_PORT_CTRL,
    PIID_PORT_INFO_A,
    PIID_PORT_INFO_C1,
    PIID_PORT_INFO_C2,
    PIID_PORT_INFO_C3,
    PIID_PROTOCOL_CTL_EXTEND,
    PIID_PROTOCOL_CTRL,
    PIID_SCENE_MODE,
    PIID_SCREEN_DIRECTION_LOCK,
    PIID_SCREEN_LANGUAGE,
    PIID_SCREEN_SAVE_TIME,
    PIID_SCREENOFF_WHILE_IDLE,
    PIID_SERIAL_NUMBER,
    PIID_USB_A_ALWAYS_ON,
    SIID_CHARGER,
    SIID_DEVICE_INFO,
    UUID_CHAR_AUTH_CTRL,
    UUID_CHAR_AUTH_DATA,
    UUID_CHAR_FIRMWARE,
    UUID_CHAR_SPEC_CMD,
    UUID_CHAR_SPEC_NOTIFY,
    ExtendedProtocolSwitches,
    LegacyProtocolSwitches,
    SceneMode,
    ScreenLanguage,
    ScreenTimeout,
    encode_dual_protocol,
    encode_port_info,
    encode_port_mask,
    encode_protocol_ctl_extend,
    encode_protocol_ctrl_v1,
)
from .parcel import DEFAULT_DMTU, ParcelChannel, ReceivedParcel
from .spec import SpecOpcode, SpecValueType, decode_value, encode_value

MOCK_DEFAULT_TOKEN = "00112233445566778899aabb"


class MockChargerTransport:
    """In-memory BleTransport implementing the peripheral side of Cuktech 10 Ultra."""

    def __init__(
        self,
        token_hex: str = MOCK_DEFAULT_TOKEN,
        *,
        push_interval: float = 1.5,
        firmware: str = "2.1.2_0045",
    ) -> None:
        self._token = parse_token_hex(token_hex)
        self._push_interval = push_interval
        self._firmware = firmware
        self._connected = False

        self._notify_cbs: dict[str, Callable[[bytes], None]] = {}
        self._auth_channel: ParcelChannel | None = None
        self._spec_cmd_channel: ParcelChannel | None = None
        self._spec_notify_channel: ParcelChannel | None = None

        self._app_rand: bytes | None = None
        self._dev_rand: bytes | None = None
        self._keys: SessionKeys | None = None
        self._dev_counter = 0
        self._push_task: asyncio.Task[Any] | None = None
        self._t0 = time.monotonic()

        self._props: dict[tuple[int, int], tuple[SpecValueType, Any]] = {}
        self._init_default_props()

    def _init_default_props(self) -> None:
        self._props = {
            (SIID_DEVICE_INFO, PIID_MANUFACTURER): (SpecValueType.STRING, "CUKTECH"),
            (SIID_DEVICE_INFO, PIID_MODEL): (SpecValueType.STRING, "njcuk.fitting.ad1204"),
            (SIID_DEVICE_INFO, PIID_SERIAL_NUMBER): (SpecValueType.STRING, "AD1204U-SIM-0001"),
            (SIID_DEVICE_INFO, PIID_FIRMWARE_REVISION): (SpecValueType.STRING, self._firmware),
            (SIID_CHARGER, PIID_PORT_INFO_C1): (SpecValueType.UINT32, encode_port_info(20.0, 3.2, 7, 1)),
            (SIID_CHARGER, PIID_PORT_INFO_C2): (SpecValueType.UINT32, encode_port_info(9.0, 2.8, 8, 1)),
            (SIID_CHARGER, PIID_PORT_INFO_C3): (SpecValueType.UINT32, encode_port_info(5.0, 1.4, 1, 1)),
            (SIID_CHARGER, PIID_PORT_INFO_A): (SpecValueType.UINT32, encode_port_info(0.0, 0.0, 0, 0)),
            (SIID_CHARGER, PIID_SCENE_MODE): (SpecValueType.UINT8, int(SceneMode.AI_SMART)),
            (SIID_CHARGER, PIID_SCREEN_SAVE_TIME): (SpecValueType.UINT8, int(ScreenTimeout.MIN_5)),
            (SIID_CHARGER, PIID_PROTOCOL_CTRL): (
                SpecValueType.UINT8,
                encode_protocol_ctrl_v1(LegacyProtocolSwitches(scp=True, mipps=True, ufcs=True)),
            ),
            (SIID_CHARGER, PIID_DELAY_DOWN_SETTING): (SpecValueType.UINT8, 0),
            (SIID_CHARGER, PIID_DELAY_TIME_C1): (SpecValueType.UINT16, 0),
            (SIID_CHARGER, PIID_DELAY_TIME_C2): (SpecValueType.UINT16, 0),
            (SIID_CHARGER, PIID_DELAY_TIME_C3): (SpecValueType.UINT16, 0),
            (SIID_CHARGER, PIID_DELAY_TIME_A): (SpecValueType.UINT16, 0),
            (SIID_CHARGER, PIID_SCREEN_LANGUAGE): (SpecValueType.UINT8, int(ScreenLanguage.CHINESE)),
            (SIID_CHARGER, PIID_USB_A_ALWAYS_ON): (SpecValueType.BOOL, False),
            (SIID_CHARGER, PIID_PORT_CTRL): (SpecValueType.UINT8, encode_port_mask(True, True, True, True)),
            (SIID_CHARGER, PIID_C1_C2_PROTOCOL): (SpecValueType.UINT32, encode_dual_protocol(7, 65, 8, 27)),
            (SIID_CHARGER, PIID_C3_A_PROTOCOL): (SpecValueType.UINT32, encode_dual_protocol(1, 10, 0, 0)),
            (SIID_CHARGER, PIID_SCREENOFF_WHILE_IDLE): (SpecValueType.UINT8, 1),
            (SIID_CHARGER, PIID_SCREEN_DIRECTION_LOCK): (SpecValueType.UINT8, 0),
            (SIID_CHARGER, PIID_PROTOCOL_CTL_EXTEND): (
                SpecValueType.UINT32,
                encode_protocol_ctl_extend(ExtendedProtocolSwitches()),
            ),
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def _emit_notify(self, uuid: str, data: bytes) -> None:
        cb = self._notify_cbs.get(uuid)
        if cb is not None:
            cb(data)

    async def connect(self) -> None:
        self._connected = True
        self._keys = None
        self._dev_counter = 0
        self._auth_channel = ParcelChannel(
            "mock-0019",
            lambda d: self._emit_notify(UUID_CHAR_AUTH_DATA, d),
            dmtu=DEFAULT_DMTU,
            on_parcel=self._on_auth_parcel,
        )
        self._spec_cmd_channel = ParcelChannel(
            "mock-001A",
            lambda d: self._emit_notify(UUID_CHAR_SPEC_CMD, d),
            dmtu=DEFAULT_DMTU,
            on_parcel=self._on_spec_cmd_parcel,
        )
        self._spec_notify_channel = ParcelChannel(
            "mock-001B",
            lambda d: self._emit_notify(UUID_CHAR_SPEC_NOTIFY, d),
            dmtu=DEFAULT_DMTU,
        )

    async def disconnect(self) -> None:
        self._connected = False
        if self._push_task:
            self._push_task.cancel()
            self._push_task = None
        self._notify_cbs.clear()

    async def read_gatt_char(self, uuid: str) -> bytes:
        if uuid == UUID_CHAR_FIRMWARE:
            return self._firmware.encode("utf-8")
        return b""

    async def start_notify(self, uuid: str, callback: Callable[[bytes], None]) -> None:
        self._notify_cbs[uuid] = callback

    async def stop_notify(self, uuid: str) -> None:
        self._notify_cbs.pop(uuid, None)

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        if uuid == UUID_CHAR_AUTH_CTRL:
            if data == AUTH_CMD_LOGIN_START:
                self._app_rand = None
                self._dev_rand = os.urandom(16)
            return
        if uuid == UUID_CHAR_AUTH_DATA and self._auth_channel:
            await self._auth_channel.handle_notification(data)
            return
        if uuid == UUID_CHAR_SPEC_CMD and self._spec_cmd_channel:
            await self._spec_cmd_channel.handle_notification(data)
            return
        if uuid == UUID_CHAR_SPEC_NOTIFY and self._spec_notify_channel:
            await self._spec_notify_channel.handle_notification(data)
            return

    async def _on_auth_parcel(self, parcel: ReceivedParcel) -> None:
        assert self._auth_channel is not None
        if parcel.data_type == PARCEL_TYPE_APP_RAND:
            self._app_rand = parcel.payload
            self._dev_rand = self._dev_rand or os.urandom(16)
            self._keys = derive_session_keys(self._token, self._app_rand, self._dev_rand)
            dev_confirm = compute_dev_confirm(self._keys.dev_key, self._dev_rand, self._app_rand)
            asyncio.create_task(self._send_dev_auth_reply(self._dev_rand, dev_confirm))
        elif parcel.data_type == PARCEL_TYPE_APP_CONFIRM:
            assert self._keys and self._app_rand and self._dev_rand
            expected = compute_app_confirm(self._keys.app_key, self._app_rand, self._dev_rand)
            ok = parcel.payload == expected
            await self._emit_notify(UUID_CHAR_AUTH_CTRL, bytes.fromhex("21000000" if ok else "23000000"))

    async def _send_dev_auth_reply(self, dev_rand: bytes, dev_confirm: bytes) -> None:
        assert self._auth_channel is not None
        await self._auth_channel.send(PARCEL_TYPE_DEV_RAND, dev_rand)
        await self._auth_channel.send(PARCEL_TYPE_DEV_CONFIRM, dev_confirm)

    def _decrypt_from_app(self, payload: bytes) -> bytes:
        assert self._keys is not None
        counter_lo2 = payload[0:2]
        nonce = self._keys.app_iv + b"\x00\x00\x00\x00" + counter_lo2 + b"\x00\x00"
        return AESCCM(self._keys.app_key, tag_length=MIC_TAG_LENGTH).decrypt(nonce, payload[2:], None)

    def _encrypt_to_app(self, plaintext: bytes) -> bytes:
        assert self._keys is not None
        cnt = self._dev_counter
        self._dev_counter = (self._dev_counter + 1) & 0xFFFF
        nonce = self._keys.dev_iv + b"\x00\x00\x00\x00" + struct.pack("<I", cnt)
        ct = AESCCM(self._keys.dev_key, tag_length=MIC_TAG_LENGTH).encrypt(nonce, plaintext, None)
        return struct.pack("<H", cnt) + ct

    async def _on_spec_cmd_parcel(self, parcel: ReceivedParcel) -> None:
        assert self._spec_cmd_channel is not None
        plain = self._decrypt_from_app(parcel.payload)
        header, tid, opcode = struct.unpack_from("<HHB", plain, 0)
        version = (header >> 12) & 0x0F
        body = plain[5 : (header & 0x0FFF)]

        if opcode == SpecOpcode.PROTOCOL_EXCHANGE:
            rsp = self._wrap_frame(2, tid, SpecOpcode.PROTOCOL_EXCHANGE, b"")
        elif opcode == SpecOpcode.GET_PROPERTIES:
            rsp = self._handle_get_properties(version, tid, body)
        elif opcode == SpecOpcode.SET_PROPERTIES:
            rsp = self._handle_set_properties(version, tid, body)
        elif opcode == SpecOpcode.ACTION:
            rsp = self._handle_action(version, tid, body)
        else:
            return

        enc = self._encrypt_to_app(rsp)
        asyncio.create_task(self._spec_cmd_channel.send(PARCEL_TYPE_SPEC, enc))

    @staticmethod
    def _wrap_frame(version: int, tid: int, opcode: SpecOpcode, body: bytes) -> bytes:
        total_len = 5 + len(body)
        header = ((version & 0x0F) << 12) | (total_len & 0x0FFF)
        return struct.pack("<HHB", header, tid, int(opcode)) + body

    def _handle_get_properties(self, version: int, tid: int, body: bytes) -> bytes:
        count = body[0]
        offset = 1
        out = bytearray([count])
        for _ in range(count):
            siid, piid = struct.unpack_from("<BH", body, offset)
            offset += 3
            entry = self._props.get((siid, piid))
            if entry is None:
                out.extend(struct.pack("<BHh", siid, piid, -4001))
            else:
                vtype, val = entry
                encoded = encode_value(vtype, val)
                type_len = ((int(vtype) & 0x0F) << 12) | (len(encoded) & 0x0FFF)
                out.extend(struct.pack("<BHhH", siid, piid, 0, type_len))
                out.extend(encoded)
        return self._wrap_frame(version, tid, SpecOpcode.GET_PROPERTIES_RSP, bytes(out))

    def _handle_set_properties(self, version: int, tid: int, body: bytes) -> bytes:
        count = body[0]
        offset = 1
        out = bytearray([count])
        for _ in range(count):
            siid, piid, type_len = struct.unpack_from("<BHH", body, offset)
            offset += 5
            vtype = SpecValueType((type_len >> 12) & 0x0F)
            vlen = type_len & 0x0FFF
            val = decode_value(vtype, body[offset : offset + vlen])
            offset += vlen

            if (siid, piid) == (SIID_CHARGER, PIID_ENTER):
                if int(val) == 2 and self._push_interval > 0 and self._push_task is None:
                    self._push_task = asyncio.create_task(self._telemetry_push_loop())
            else:
                self._props[(siid, piid)] = (vtype, val)
                # Keep port output reflected when toggling port_ctrl
                if (siid, piid) == (SIID_CHARGER, PIID_PORT_CTRL):
                    self._apply_port_ctrl_to_telemetry(int(val))
            out.extend(struct.pack("<BHh", siid, piid, 0))
        return self._wrap_frame(version, tid, SpecOpcode.SET_PROPERTIES_RSP, bytes(out))

    def _apply_port_ctrl_to_telemetry(self, mask: int) -> None:
        defaults = [
            (PIID_PORT_INFO_C1, 20.0, 3.2, 7),
            (PIID_PORT_INFO_C2, 9.0, 2.8, 8),
            (PIID_PORT_INFO_C3, 5.0, 1.4, 1),
            (PIID_PORT_INFO_A, 5.0, 0.5, 1),
        ]
        for idx, (piid, v, i, proto) in enumerate(defaults):
            enabled = bool(mask & (1 << idx))
            if enabled:
                if idx == 3:
                    self._props[(SIID_CHARGER, piid)] = (SpecValueType.UINT32, encode_port_info(0.0, 0.0, 0, 0))
                else:
                    self._props[(SIID_CHARGER, piid)] = (SpecValueType.UINT32, encode_port_info(v, i, proto, 1))
            else:
                self._props[(SIID_CHARGER, piid)] = (SpecValueType.UINT32, encode_port_info(0.0, 0.0, 0, 0))

    def _handle_action(self, version: int, tid: int, body: bytes) -> bytes:
        siid, aiid, _count = struct.unpack_from("<BBB", body, 0)
        if (siid, aiid) == (SIID_CHARGER, AIID_RESET_SETTING):
            self._init_default_props()
            return self._wrap_frame(version, tid, SpecOpcode.ACTION_RSP, struct.pack("<hB", 0, 0))
        return self._wrap_frame(version, tid, SpecOpcode.ACTION_RSP, struct.pack("<h", -4001))

    async def _telemetry_push_loop(self) -> None:
        """Simulate realistic live wattage fluctuation pushed over 001B (opcode 0x04)."""
        try:
            while self._connected and self._spec_notify_channel is not None:
                await asyncio.sleep(self._push_interval)
                if not self._connected:
                    break
                t = time.monotonic() - self._t0
                mask = int(self._props[(SIID_CHARGER, PIID_PORT_CTRL)][1])

                c1_i = round(3.0 + 0.4 * math.sin(t * 0.7), 1) if (mask & 0x01) else 0.0
                c2_i = round(2.5 + 0.3 * math.cos(t * 0.5), 1) if (mask & 0x02) else 0.0
                c3_i = round(1.2 + 0.2 * math.sin(t * 1.1), 1) if (mask & 0x04) else 0.0

                c1_raw = encode_port_info(20.0 if c1_i else 0.0, c1_i, 7 if c1_i else 0, 1 if c1_i else 0)
                c2_raw = encode_port_info(9.0 if c2_i else 0.0, c2_i, 8 if c2_i else 0, 1 if c2_i else 0)
                c3_raw = encode_port_info(5.0 if c3_i else 0.0, c3_i, 1 if c3_i else 0, 1 if c3_i else 0)

                self._props[(SIID_CHARGER, PIID_PORT_INFO_C1)] = (SpecValueType.UINT32, c1_raw)
                self._props[(SIID_CHARGER, PIID_PORT_INFO_C2)] = (SpecValueType.UINT32, c2_raw)
                self._props[(SIID_CHARGER, PIID_PORT_INFO_C3)] = (SpecValueType.UINT32, c3_raw)

                # Encode opcode 0x04 properties_changed frame
                body = bytearray([3])
                for piid, raw in (
                    (PIID_PORT_INFO_C1, c1_raw),
                    (PIID_PORT_INFO_C2, c2_raw),
                    (PIID_PORT_INFO_C3, c3_raw),
                ):
                    val_bytes = struct.pack("<I", raw)
                    type_len = (int(SpecValueType.UINT32) << 12) | 4
                    body.extend(struct.pack("<BHH", SIID_CHARGER, piid, type_len))
                    body.extend(val_bytes)

                frame = self._wrap_frame(2, 0, SpecOpcode.PROPERTIES_CHANGED, bytes(body))
                enc = self._encrypt_to_app(frame)
                await self._spec_notify_channel.send(PARCEL_TYPE_SPEC, enc)
        except asyncio.CancelledError:
            return
