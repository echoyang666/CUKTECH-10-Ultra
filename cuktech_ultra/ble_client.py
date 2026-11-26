"""Async BLE client for CUKTECH 10 GaN Charger Ultra (njcuk.fitting.ad1204).

Implements the full standalone offline stack over BLE GATT Service 0xFE95:
  BLE GATT -> MiBLE Parcel -> StandardAuth + AES-128-CCM -> MIoT-BLE Spec v2 -> Cuktech 10 Ultra
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .crypto import (
    CryptoError,
    MscCipher,
    compute_app_confirm,
    derive_session_keys,
    generate_app_rand,
    parse_token_hex,
    verify_dev_confirm,
)
from .device import (
    AIID_RESET_SETTING,
    PIID_DELAY_DOWN_SETTING,
    PIID_PORT_CTRL,
    PIID_PROTOCOL_CTL_EXTEND,
    PIID_PROTOCOL_CTRL,
    PIID_SCENE_MODE,
    PIID_SCREEN_DIRECTION_LOCK,
    PIID_SCREEN_LANGUAGE,
    PIID_SCREEN_SAVE_TIME,
    PIID_SCREENOFF_WHILE_IDLE,
    PIID_USB_A_ALWAYS_ON,
    PORT_DELAY_PIID,
    PORT_NAMES,
    SIID_CHARGER,
    TARGET_PDID,
    UUID_CHAR_AUTH_CTRL,
    UUID_CHAR_AUTH_DATA,
    UUID_CHAR_FIRMWARE,
    UUID_CHAR_SPEC_CMD,
    UUID_CHAR_SPEC_NOTIFY,
    UUID_SERVICE_FE95,
    ChargerState,
    ExtendedProtocolSwitches,
    LegacyProtocolSwitches,
    SceneMode,
    ScreenLanguage,
    ScreenTimeout,
    build_device_info_reads,
    build_enter_app_write,
    build_settings_reads,
    build_telemetry_reads,
    encode_port_mask,
    encode_protocol_ctl_extend,
    encode_protocol_ctrl_v1,
    parse_mibeacon_fe95,
)
from .parcel import DEFAULT_DMTU, ParcelChannel, ReceivedParcel
from .spec import (
    ActionArg,
    PropertyReadReq,
    PropertyValue,
    PropertyWriteReq,
    SpecFrame,
    SpecOpcode,
    SpecValueType,
    decode_frame,
    encode_action,
    encode_get_properties,
    encode_protocol_exchange,
    encode_set_properties,
)

logger = logging.getLogger(__name__)

# Standard Auth control bytes on 0010
AUTH_CMD_LOGIN_START = bytes.fromhex("24000000")
AUTH_RSP_LOGIN_OK_PREFIX = 0x21
AUTH_RSP_LOGIN_FAIL_PREFIX = 0x23

# Standard Auth parcel types on 0019
PARCEL_TYPE_SPEC = 0x00
PARCEL_TYPE_APP_CONFIRM = 0x0A
PARCEL_TYPE_APP_RAND = 0x0B
PARCEL_TYPE_DEV_CONFIRM = 0x0C
PARCEL_TYPE_DEV_RAND = 0x0D


class BleTransport(Protocol):
    """Minimal async BLE GATT transport interface (implemented by BleakTransport & MockChargerTransport)."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    @property
    def is_connected(self) -> bool: ...
    async def read_gatt_char(self, uuid: str) -> bytes: ...
    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None: ...
    async def start_notify(self, uuid: str, callback: Callable[[bytes], None]) -> None: ...
    async def stop_notify(self, uuid: str) -> None: ...


@dataclass(frozen=True)
class DiscoveredCharger:
    address: str
    name: str
    rssi: int
    mac: str | None
    pdid: int | None
    likely_cuktech_10_ultra: bool


def _normalize_mac(mac: str) -> str:
    cleaned = mac.strip().upper().replace("-", ":")
    if ":" not in cleaned and len(cleaned) == 12:
        cleaned = ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
    return cleaned


async def _get_os_connected_fe95_devices() -> list[Any]:
    """Query OS-level already-connected BLE peripherals exposing 0xFE95 (e.g. macOS CoreBluetooth)."""
    try:
        from bleak import BleakAdapter

        adapter = await BleakAdapter.get()
        return await adapter.get_connected_devices(service_uuids=[UUID_SERVICE_FE95])
    except Exception as exc:
        logger.debug("BleakAdapter.get_connected_devices unavailable: %s", exc)
        return []


async def scan_chargers(timeout: float = 8.0) -> list[DiscoveredCharger]:
    """Scan nearby BLE devices advertising Xiaomi 0xFE95 service data or Cuktech names,
    and also include any 0xFE95 devices already connected at the OS level.
    """
    from bleak import BleakScanner

    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results: list[DiscoveredCharger] = []
    seen_addrs: set[str] = set()

    for device, adv in discovered.values():
        svc_data = adv.service_data or {}
        fe95_raw = None
        for k, v in svc_data.items():
            if k.lower() in (UUID_SERVICE_FE95, "fe95", "0000fe95"):
                fe95_raw = bytes(v)
                break

        beacon = parse_mibeacon_fe95(fe95_raw) if fe95_raw else None
        name = device.name or adv.local_name or ""
        is_fe95 = beacon is not None or any(
            u.lower() == UUID_SERVICE_FE95 for u in (adv.service_uuids or [])
        )
        name_match = any(kw in name.lower() for kw in ("cuktech", "ad1204", "njcuk", "10 ultra"))

        if not (is_fe95 or name_match):
            continue

        likely = bool((beacon and beacon.pdid == TARGET_PDID) or name_match)
        seen_addrs.add(device.address.upper())
        results.append(
            DiscoveredCharger(
                address=device.address,
                name=name or "(unnamed)",
                rssi=adv.rssi if adv.rssi is not None else -127,
                mac=beacon.mac if beacon else None,
                pdid=beacon.pdid if beacon else None,
                likely_cuktech_10_ultra=likely,
            )
        )

    for dev in await _get_os_connected_fe95_devices():
        if dev.address.upper() in seen_addrs:
            continue
        name = dev.name or "(OS-connected 0xFE95)"
        likely = any(kw in name.lower() for kw in ("cuktech", "ad1204", "njcuk", "10 ultra"))
        results.append(
            DiscoveredCharger(
                address=dev.address,
                name=f"{name} [OS已连]",
                rssi=0,
                mac=None,
                pdid=None,
                likely_cuktech_10_ultra=likely,
            )
        )

    results.sort(key=lambda r: (not r.likely_cuktech_10_ultra, -r.rssi))
    return results


class BleakTransport:
    """Real BLE transport backed by bleak (CoreBluetooth on macOS, BlueZ on Linux, WinRT on Windows)."""

    def __init__(
        self,
        *,
        address: str | None = None,
        mac: str | None = None,
        scan_timeout: float = 10.0,
    ) -> None:
        self._address = address
        self._mac = _normalize_mac(mac) if mac else None
        self._scan_timeout = scan_timeout
        self._client: Any = None
        self.resolved_address: str | None = address

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def _resolve_device(self) -> Any:
        from bleak import BleakScanner

        # Step 0: Check OS-level already-connected 0xFE95 peripherals (works even when not advertising!)
        for dev in await _get_os_connected_fe95_devices():
            addr_upper = (dev.address or "").upper()
            name_lower = (dev.name or "").lower()
            if self._address and addr_upper == self._address.upper():
                logger.info("Found OS-connected BLE peripheral by address: %s (%s)", dev.name, dev.address)
                self.resolved_address = dev.address
                return dev
            if any(kw in name_lower for kw in ("ad1204", "cuktech", "njcuk")):
                logger.info("Found OS-connected BLE peripheral by model name: %s (%s)", dev.name, dev.address)
                self.resolved_address = dev.address
                return dev

        loop = asyncio.get_running_loop()
        found_fut: asyncio.Future[Any] = loop.create_future()
        fallback_dev: Any = None

        def _on_detect(device: Any, adv: Any) -> None:
            nonlocal fallback_dev
            if found_fut.done():
                return

            addr_upper = (device.address or "").upper()
            if self._address and addr_upper == self._address.upper():
                self.resolved_address = device.address
                found_fut.set_result(device)
                return

            svc_data = adv.service_data or {}
            fe95_raw = None
            for k, v in svc_data.items():
                if k.lower() in (UUID_SERVICE_FE95, "fe95", "0000fe95"):
                    fe95_raw = bytes(v)
                    break

            beacon = parse_mibeacon_fe95(fe95_raw) if fe95_raw else None
            if self._mac:
                if beacon and beacon.mac and _normalize_mac(beacon.mac) == self._mac:
                    self.resolved_address = device.address
                    found_fut.set_result(device)
                    return
                if _normalize_mac(addr_upper) == self._mac:
                    self.resolved_address = device.address
                    found_fut.set_result(device)
                    return

            # Keep a fallback candidate if advertising our exact model/PDID without MAC in this frame
            name = (device.name or adv.local_name or "").lower()
            if (beacon and beacon.pdid == TARGET_PDID) or "ad1204" in name or "cuktech" in name:
                fallback_dev = device

        logger.info(
            "Scanning BLE for target (address=%s, mac=%s, timeout=%.1fs)...",
            self._address or "-",
            self._mac or "-",
            self._scan_timeout,
        )
        scanner = BleakScanner(detection_callback=_on_detect)
        await scanner.start()
        try:
            wait_s = min(self._scan_timeout, 4.0) if self._address else self._scan_timeout
            try:
                return await asyncio.wait_for(found_fut, timeout=wait_s)
            except TimeoutError:
                if fallback_dev is not None:
                    logger.info(
                        "Using PDID/model match %s (%s) after MAC wait timeout",
                        fallback_dev.name,
                        fallback_dev.address,
                    )
                    self.resolved_address = fallback_dev.address
                    return fallback_dev
                if self._address:
                    logger.info("Falling back to direct BLE address connect: %s", self._address)
                    return self._address
                raise ConnectionError(
                    f"Could not find BLE charger (mac={self._mac}, address={self._address}) "
                    f"within {self._scan_timeout}s. Make sure the charger is powered on, nearby, "
                    "and not currently connected to Mi Home or another cuktech-ultra instance."
                )
        finally:
            await scanner.stop()

    async def connect(self) -> None:
        from bleak import BleakClient

        target = await self._resolve_device()
        self._client = BleakClient(target, timeout=15.0)
        await self._client.connect()

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    async def read_gatt_char(self, uuid: str) -> bytes:
        assert self._client is not None
        return bytes(await self._client.read_gatt_char(uuid))

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        assert self._client is not None
        # Auto-align write mode with the characteristic's actual GATT properties (important on CoreBluetooth)
        effective_response = response
        try:
            char = self._client.services.get_characteristic(uuid)
            if char is not None:
                props = set(char.properties)
                if not response and "write-without-response" not in props and "write" in props:
                    effective_response = True
                elif response and "write" not in props and "write-without-response" in props:
                    effective_response = False
        except Exception:
            pass
        await self._client.write_gatt_char(uuid, data, response=effective_response)

    async def start_notify(self, uuid: str, callback: Callable[[bytes], None]) -> None:
        assert self._client is not None

        def _wrapper(_sender: Any, data: bytearray) -> None:
            callback(bytes(data))

        await self._client.start_notify(uuid, _wrapper)

    async def stop_notify(self, uuid: str) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(uuid)
            except Exception:
                pass


class CuktechUltraClient:
    """High-level standalone client for CUKTECH 10 GaN Charger Ultra."""

    def __init__(
        self,
        transport: BleTransport,
        token_hex: str,
        *,
        dmtu: int = DEFAULT_DMTU,
        request_timeout: float = 10.0,
    ) -> None:
        self._transport = transport
        self._token = parse_token_hex(token_hex)
        self._dmtu = dmtu
        self._request_timeout = request_timeout

        self.state = ChargerState()
        self._cipher: MscCipher | None = None
        self._spec_version = 2
        self._tid = 1

        self._auth_ctrl_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._auth_channel: ParcelChannel | None = None
        self._spec_cmd_channel: ParcelChannel | None = None
        self._spec_notify_channel: ParcelChannel | None = None

        self._pending: dict[int, asyncio.Future[SpecFrame]] = {}
        self._spec_lock = asyncio.Lock()
        self._listeners: list[Callable[[ChargerState], Awaitable[None] | None]] = []
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    @property
    def is_connected(self) -> bool:
        return self._transport.is_connected and self._cipher is not None

    def add_listener(self, callback: Callable[[ChargerState], Awaitable[None] | None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[ChargerState], Awaitable[None] | None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _emit_state(self) -> None:
        for cb in list(self._listeners):
            try:
                res = cb(self.state)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.warning("State listener error: %s", exc)

    def _next_tid(self) -> int:
        tid = self._tid
        self._tid = (self._tid % 0xFFFE) + 1
        return tid

    async def connect(self, *, fetch_initial_state: bool = True) -> ChargerState:
        """Connect over BLE, perform MiBLE Standard Auth login, exchange Spec version, and sync state."""
        await self._transport.connect()
        try:
            self._auth_ctrl_queue = asyncio.Queue()
            self._auth_channel = ParcelChannel(
                "0019",
                lambda data: self._transport.write_gatt_char(UUID_CHAR_AUTH_DATA, data, response=False),
                dmtu=self._dmtu,
            )
            self._spec_cmd_channel = ParcelChannel(
                "001A",
                lambda data: self._transport.write_gatt_char(UUID_CHAR_SPEC_CMD, data, response=False),
                dmtu=self._dmtu,
                on_parcel=self._on_spec_parcel,
            )
            self._spec_notify_channel = ParcelChannel(
                "001B",
                lambda data: self._transport.write_gatt_char(UUID_CHAR_SPEC_NOTIFY, data, response=False),
                dmtu=self._dmtu,
                on_parcel=self._on_spec_parcel,
            )

            # Step 1: Subscribe 0019 and 0010 notifications
            await self._transport.start_notify(
                UUID_CHAR_AUTH_DATA,
                lambda raw: self._spawn(self._auth_channel.handle_notification(raw)),  # type: ignore[union-attr]
            )
            await self._transport.start_notify(
                UUID_CHAR_AUTH_CTRL,
                lambda raw: self._auth_ctrl_queue.put_nowait(raw),
            )

            # Step 2: Optional firmware read on 0004
            try:
                fw_raw = await self._transport.read_gatt_char(UUID_CHAR_FIRMWARE)
                fw_str = fw_raw.decode("utf-8", errors="ignore").strip("\x00 \r\n")
                if fw_str:
                    self.state.firmware_revision = fw_str
                    logger.info("Read firmware version (0004): %s", fw_str)
            except Exception as exc:
                logger.debug("Optional read 0004 failed (continuing): %s", exc)

            # Step 3: Standard Auth login handshake
            await self._perform_standard_auth_login()

            # Step 4: Subscribe 001A (Spec cmd/rsp) and 001B (Spec unsolicited notify)
            await self._transport.start_notify(
                UUID_CHAR_SPEC_CMD,
                lambda raw: self._spawn(self._spec_cmd_channel.handle_notification(raw)),  # type: ignore[union-attr]
            )
            await self._transport.start_notify(
                UUID_CHAR_SPEC_NOTIFY,
                lambda raw: self._spawn(self._spec_notify_channel.handle_notification(raw)),  # type: ignore[union-attr]
            )

            # Step 5: MIoT-BLE Spec protocol_exchange (0xF0)
            await self._exchange_protocol_version()

            self.state.connected = True

            # Step 6: Notify device enter=2 (2.14) and load initial state
            if fetch_initial_state:
                try:
                    await self.send_enter_app()
                except Exception as exc:
                    logger.warning("send_enter_app (2.14=2) warning: %s", exc)
                await self.refresh()

            await self._emit_state()
            return self.state
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        self.state.connected = False
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._cipher = None

        for uuid in (
            UUID_CHAR_SPEC_NOTIFY,
            UUID_CHAR_SPEC_CMD,
            UUID_CHAR_AUTH_CTRL,
            UUID_CHAR_AUTH_DATA,
        ):
            try:
                await self._transport.stop_notify(uuid)
            except Exception:
                pass
        await self._transport.disconnect()
        await self._emit_state()

    async def _perform_standard_auth_login(self) -> None:
        assert self._auth_channel is not None

        # 1. Write 24 00 00 00 to 0010
        logger.debug("StandardAuth: writing login start 24000000 to 0010")
        await self._transport.write_gatt_char(UUID_CHAR_AUTH_CTRL, AUTH_CMD_LOGIN_START, response=False)

        # 2. Send 16B app_rand via 0019 (type 0x0B)
        app_rand = generate_app_rand()
        await self._auth_channel.send(PARCEL_TYPE_APP_RAND, app_rand)

        # 3. Receive dev_rand (0x0D) and dev_confirm (0x0C) in either order
        received: dict[int, bytes] = {}
        while PARCEL_TYPE_DEV_RAND not in received or PARCEL_TYPE_DEV_CONFIRM not in received:
            parcel = await self._auth_channel.receive(timeout=self._request_timeout)
            if parcel.data_type in (PARCEL_TYPE_DEV_RAND, PARCEL_TYPE_DEV_CONFIRM):
                received[parcel.data_type] = parcel.payload
            else:
                logger.debug("Ignoring unexpected 0019 parcel type=0x%02x", parcel.data_type)

        dev_rand = received[PARCEL_TYPE_DEV_RAND]
        dev_confirm = received[PARCEL_TYPE_DEV_CONFIRM]

        # 4. Derive session keys & verify dev_confirm
        keys = derive_session_keys(self._token, app_rand, dev_rand)
        if not verify_dev_confirm(keys.dev_key, dev_rand, app_rand, dev_confirm):
            raise CryptoError(
                "StandardAuth failed: device confirmation HMAC mismatch. "
                "Verify that the 12/16-byte device token matches this charger."
            )

        # 5. Send app_confirm (0x0A)
        app_confirm = compute_app_confirm(keys.app_key, app_rand, dev_rand)
        await self._auth_channel.send(PARCEL_TYPE_APP_CONFIRM, app_confirm)

        # 6. Wait for login result on 0010 (21 00 00 00)
        result = await asyncio.wait_for(self._auth_ctrl_queue.get(), timeout=self._request_timeout)
        if not result or result[0] != AUTH_RSP_LOGIN_OK_PREFIX:
            raise CryptoError(f"StandardAuth login rejected by device on 0010: {result.hex()}")

        self._cipher = MscCipher(keys, initial_app_counter=0)
        logger.info("StandardAuth login succeeded")

    async def _on_spec_parcel(self, parcel: ReceivedParcel) -> None:
        if self._cipher is None:
            return
        try:
            plaintext = self._cipher.decrypt_from_device(parcel.payload)
        except Exception as exc:
            logger.warning("Failed to decrypt Spec parcel (%s): %s", parcel.payload.hex(), exc)
            return

        logger.debug("Spec RX plaintext <= %s", plaintext.hex())
        try:
            frame = decode_frame(plaintext)
        except Exception as exc:
            logger.warning("Failed to decode Spec frame (%s): %s", plaintext.hex(), exc)
            return

        logger.debug("Spec RX opcode=%s tid=%d props=%s", frame.opcode.name, frame.tid, frame.properties)

        if frame.opcode == SpecOpcode.PROPERTIES_CHANGED:
            for prop in frame.properties:
                self.state.apply_property(prop)
            await self._emit_state()
            return

        if frame.opcode == SpecOpcode.EVENT_OCCURED:
            logger.info("Spec event siid=%s eiid=%s args=%s", frame.event_siid, frame.event_eiid, frame.event_args)
            return

        fut = self._pending.get(frame.tid)
        if fut is not None and not fut.done():
            fut.set_result(frame)
        else:
            # Fallback for protocol_exchange responses that might echo tid=0
            if frame.opcode == SpecOpcode.PROTOCOL_EXCHANGE:
                for pending_fut in self._pending.values():
                    if not pending_fut.done():
                        pending_fut.set_result(frame)
                        break

    async def _send_spec_request(self, frame_bytes: bytes, tid: int) -> SpecFrame:
        if self._cipher is None or self._spec_cmd_channel is None:
            raise ConnectionError("Client is not connected")

        async with self._spec_lock:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[SpecFrame] = loop.create_future()
            self._pending[tid] = fut
            try:
                encrypted = self._cipher.encrypt_to_device(frame_bytes)
                await self._spec_cmd_channel.send(PARCEL_TYPE_SPEC, encrypted)
                return await asyncio.wait_for(fut, timeout=self._request_timeout)
            finally:
                self._pending.pop(tid, None)

    async def _exchange_protocol_version(self) -> int:
        tid = self._next_tid()
        req = encode_protocol_exchange(tid, version=2)
        try:
            rsp = await self._send_spec_request(req, tid)
            if rsp.version >= 2:
                self._spec_version = rsp.version
            logger.info("MIoT-BLE Spec version negotiated: v%d", self._spec_version)
        except Exception as exc:
            logger.warning("protocol_exchange failed, defaulting to v2: %s", exc)
            self._spec_version = 2
        return self._spec_version

    # ------------------------------------------------------------------
    # Low-level MIoT-BLE Spec operations
    # ------------------------------------------------------------------

    async def get_properties(self, reqs: list[PropertyReadReq]) -> list[PropertyValue]:
        """Read properties in small batches (<= 6 per BLE request) and update state."""
        all_results: list[PropertyValue] = []
        batch_size = 6
        for i in range(0, len(reqs), batch_size):
            chunk = reqs[i : i + batch_size]
            tid = self._next_tid()
            frame = encode_get_properties(tid, chunk, version=self._spec_version)
            rsp = await self._send_spec_request(frame, tid)
            for prop in rsp.properties:
                self.state.apply_property(prop)
                all_results.append(prop)
        await self._emit_state()
        return all_results

    async def set_properties(self, reqs: list[PropertyWriteReq]) -> list[PropertyValue]:
        """Write properties and return per-property status codes."""
        tid = self._next_tid()
        frame = encode_set_properties(tid, reqs, version=self._spec_version)
        rsp = await self._send_spec_request(frame, tid)
        # Optimistically apply successfully written properties onto local state
        ok_keys = {(p.siid, p.piid) for p in rsp.properties if p.code == 0}
        for req in reqs:
            if (req.siid, req.piid) in ok_keys:
                self.state.apply_property(
                    PropertyValue(
                        siid=req.siid,
                        piid=req.piid,
                        code=0,
                        value_type=req.value_type,
                        value=req.value,
                    )
                )
        await self._emit_state()
        return rsp.properties

    async def do_action(
        self,
        siid: int,
        aiid: int,
        args: list[ActionArg] | None = None,
    ) -> SpecFrame:
        """Execute a MIoT-BLE Spec action (opcode 0x05)."""
        tid = self._next_tid()
        frame = encode_action(tid, siid, aiid, args, version=self._spec_version)
        return await self._send_spec_request(frame, tid)

    # ------------------------------------------------------------------
    # High-level CUKTECH 10 Ultra operations
    # ------------------------------------------------------------------

    async def send_enter_app(self) -> None:
        """Tell the charger that the client entered foreground (2.14 = 2) to trigger live telemetry."""
        await self.set_properties([build_enter_app_write()])

    async def refresh(self) -> ChargerState:
        """Fetch device info, 4-port telemetry, protocol statuses, and all charger settings."""
        await self.get_properties(build_device_info_reads())
        await self.get_properties(build_telemetry_reads())
        await self.get_properties(build_settings_reads(include_v2=self.state.spec_v2_supported))
        return self.state

    async def refresh_telemetry(self) -> ChargerState:
        """Fast refresh of only the 4-port telemetry + negotiated fast-charge protocols."""
        await self.get_properties(build_telemetry_reads())
        return self.state

    async def set_scene_mode(self, mode: SceneMode) -> list[PropertyValue]:
        return await self.set_properties(
            [PropertyWriteReq(SIID_CHARGER, PIID_SCENE_MODE, SpecValueType.UINT8, int(mode))]
        )

    async def set_port_enabled(self, port: str, enabled: bool) -> list[PropertyValue]:
        name = port.strip().lower()
        if name not in PORT_NAMES:
            raise ValueError(f"Unknown port '{port}'. Must be one of {PORT_NAMES}")
        mask = encode_port_mask(
            c1=enabled if name == "c1" else self.state.ports["c1"].enabled,
            c2=enabled if name == "c2" else self.state.ports["c2"].enabled,
            c3=enabled if name == "c3" else self.state.ports["c3"].enabled,
            a=enabled if name == "a" else self.state.ports["a"].enabled,
        )
        return await self.set_properties(
            [PropertyWriteReq(SIID_CHARGER, PIID_PORT_CTRL, SpecValueType.UINT8, mask)]
        )

    async def set_port_timer(self, port: str, minutes: int) -> list[PropertyValue]:
        """Set or cancel a port auto-off countdown timer (minutes=0 disables)."""
        name = port.strip().lower()
        if name not in PORT_NAMES:
            raise ValueError(f"Unknown port '{port}'. Must be one of {PORT_NAMES}")
        if not (0 <= minutes <= 1440):
            raise ValueError("Timer minutes must be between 0 and 1440")

        enable = minutes > 0
        delay_mask = encode_port_mask(
            c1=enable if name == "c1" else self.state.ports["c1"].delay_enabled,
            c2=enable if name == "c2" else self.state.ports["c2"].delay_enabled,
            c3=enable if name == "c3" else self.state.ports["c3"].delay_enabled,
            a=enable if name == "a" else self.state.ports["a"].delay_enabled,
        )
        return await self.set_properties(
            [
                PropertyWriteReq(SIID_CHARGER, PIID_DELAY_DOWN_SETTING, SpecValueType.UINT8, delay_mask),
                PropertyWriteReq(SIID_CHARGER, PORT_DELAY_PIID[name], SpecValueType.UINT16, minutes),
            ]
        )

    async def set_usb_a_always_on(self, enabled: bool) -> list[PropertyValue]:
        """Toggle USB-A trickle / low-current mode (auto-off after 4 hours)."""
        return await self.set_properties(
            [PropertyWriteReq(SIID_CHARGER, PIID_USB_A_ALWAYS_ON, SpecValueType.BOOL, bool(enabled))]
        )

    async def set_screen_timeout(self, timeout: ScreenTimeout) -> list[PropertyValue]:
        return await self.set_properties(
            [PropertyWriteReq(SIID_CHARGER, PIID_SCREEN_SAVE_TIME, SpecValueType.UINT8, int(timeout))]
        )

    async def set_screen_language(self, language: ScreenLanguage) -> list[PropertyValue]:
        return await self.set_properties(
            [PropertyWriteReq(SIID_CHARGER, PIID_SCREEN_LANGUAGE, SpecValueType.UINT8, int(language))]
        )

    async def set_screenoff_while_idle(self, enabled: bool) -> list[PropertyValue]:
        return await self.set_properties(
            [PropertyWriteReq(SIID_CHARGER, PIID_SCREENOFF_WHILE_IDLE, SpecValueType.UINT8, 1 if enabled else 0)]
        )

    async def set_screen_direction_lock(self, locked: bool) -> list[PropertyValue]:
        return await self.set_properties(
            [PropertyWriteReq(SIID_CHARGER, PIID_SCREEN_DIRECTION_LOCK, SpecValueType.UINT8, 1 if locked else 0)]
        )

    async def set_protocol_switches_v1(self, switches: LegacyProtocolSwitches) -> list[PropertyValue]:
        encoded = encode_protocol_ctrl_v1(switches)
        return await self.set_properties(
            [PropertyWriteReq(SIID_CHARGER, PIID_PROTOCOL_CTRL, SpecValueType.UINT8, encoded)]
        )

    async def set_protocol_switches_v2(self, switches: ExtendedProtocolSwitches) -> list[PropertyValue]:
        encoded = encode_protocol_ctl_extend(switches)
        return await self.set_properties(
            [PropertyWriteReq(SIID_CHARGER, PIID_PROTOCOL_CTL_EXTEND, SpecValueType.UINT32, encoded)]
        )

    async def reset_settings(self) -> SpecFrame:
        """Restore factory default settings (Action 2.1)."""
        rsp = await self.do_action(SIID_CHARGER, AIID_RESET_SETTING, [])
        await self.refresh()
        return rsp
