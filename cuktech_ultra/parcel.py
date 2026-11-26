"""MiBLE multi-packet / single-packet parcel framing layer (characteristics 0019, 001A, 001B)."""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_DMTU = 18  # 18B payload + 2B seq = 20B (BLE ATT MTU 23 safe)

# Control frames (prefix 00 00)
CTRL_PREFIX = b"\x00\x00"
ACK_SUCCESS = b"\x00\x00\x01\x00"
ACK_READY = b"\x00\x00\x01\x01"
ACK_BUSY = b"\x00\x00\x01\x02"
ACK_TIMEOUT = b"\x00\x00\x01\x03"
ACK_CANCEL = b"\x00\x00\x01\x04"
ACK_RETRANSMIT_PREFIX = b"\x00\x00\x01\x05"

ACK_SINGLE_SUCCESS = b"\x00\x00\x03\x00"
ACK_SINGLE_BUSY = b"\x00\x00\x03\x02"


class ParcelError(Exception):
    """Raised when parcel framing, ACK, or reassembly fails."""


def encode_cmd_start(data_type: int, total_frames: int) -> bytes:
    """Multi-pack initiation header: 00 00 00 <data_type:1B> <total_frames:u16_LE>."""
    if not (0 <= data_type <= 0xFF):
        raise ValueError(f"data_type out of range: {data_type}")
    if not (1 <= total_frames <= 0xFFFF):
        raise ValueError(f"total_frames out of range: {total_frames}")
    return b"\x00\x00\x00" + bytes([data_type]) + struct.pack("<H", total_frames)


def encode_single_pack(data_type: int, payload: bytes) -> bytes:
    """Single-pack frame: 00 00 02 <data_type:1B> <payload>."""
    if not (0 <= data_type <= 0xFF):
        raise ValueError(f"data_type out of range: {data_type}")
    return b"\x00\x00\x02" + bytes([data_type]) + payload


def split_frames(payload: bytes, dmtu: int = DEFAULT_DMTU) -> list[bytes]:
    """Split payload into 1-based sequence-numbered frames: <seq:u16_LE> <chunk>."""
    if dmtu < 1:
        raise ValueError("dmtu must be >= 1")
    if not payload:
        return [struct.pack("<H", 1)]
    frames: list[bytes] = []
    for idx, offset in enumerate(range(0, len(payload), dmtu), start=1):
        frames.append(struct.pack("<H", idx) + payload[offset : offset + dmtu])
    return frames


@dataclass
class ReceivedParcel:
    """A fully reassembled inbound parcel."""

    data_type: int
    payload: bytes


class ParcelAssembler:
    """Pure state machine for reassembling inbound MiBLE parcels (multi-pack and single-pack)."""

    def __init__(self) -> None:
        self._active = False
        self._data_type = 0
        self._expected_frames = 0
        self._chunks: dict[int, bytes] = {}

    def reset(self) -> None:
        self._active = False
        self._data_type = 0
        self._expected_frames = 0
        self._chunks.clear()

    def feed(self, packet: bytes) -> tuple[bytes | None, ReceivedParcel | None]:
        """Process one incoming BLE notification packet.

        Returns (ack_to_send, completed_parcel).
        """
        if len(packet) < 2:
            raise ParcelError(f"Incoming parcel packet too short: {packet.hex()}")

        seq = struct.unpack_from("<H", packet, 0)[0]

        if seq == 0:
            if len(packet) < 4:
                raise ParcelError(f"Control packet too short: {packet.hex()}")
            cmd = packet[2]
            if cmd == 0x00:
                # Multi-pack start: 00 00 00 <data_type:1B> <total_frames:u16_LE>
                if len(packet) < 6:
                    raise ParcelError(f"Multi-pack start header too short: {packet.hex()}")
                self._data_type = packet[3]
                self._expected_frames = struct.unpack_from("<H", packet, 4)[0]
                self._chunks.clear()
                self._active = self._expected_frames > 0
                if not self._active:
                    return ACK_SUCCESS, ReceivedParcel(self._data_type, b"")
                return ACK_READY, None

            if cmd == 0x02:
                # Single-pack: 00 00 02 <data_type:1B> <payload...>
                data_type = packet[3]
                payload = bytes(packet[4:])
                self.reset()
                return ACK_SINGLE_SUCCESS, ReceivedParcel(data_type, payload)

            # Other control packets (0x01 / 0x03) are ACKs for our outbound writes, not inbound RX.
            return None, None

        # Data frame (seq >= 1)
        if not self._active:
            logger.debug("Ignoring unexpected parcel data frame seq=%d while idle", seq)
            return None, None

        self._chunks[seq] = bytes(packet[2:])
        if len(self._chunks) >= self._expected_frames:
            # Verify contiguous 1..expected_frames
            missing = [i for i in range(1, self._expected_frames + 1) if i not in self._chunks]
            if missing:
                retransmit = ACK_RETRANSMIT_PREFIX + struct.pack("<H", missing[0])
                return retransmit, None

            data = b"".join(self._chunks[i] for i in range(1, self._expected_frames + 1))
            parcel = ReceivedParcel(self._data_type, data)
            self.reset()
            return ACK_SUCCESS, parcel

        return None, None


class ParcelChannel:
    """Async MiBLE parcel transceiver bound to a single BLE characteristic."""

    def __init__(
        self,
        name: str,
        write_fn: Callable[[bytes], Awaitable[None]],
        *,
        dmtu: int = DEFAULT_DMTU,
        ack_timeout: float = 8.0,
        on_parcel: Callable[[ReceivedParcel], Awaitable[None] | None] | None = None,
    ) -> None:
        self.name = name
        self._write_fn = write_fn
        self.dmtu = dmtu
        self.ack_timeout = ack_timeout
        self._on_parcel = on_parcel

        self._assembler = ParcelAssembler()
        self._rx_queue: asyncio.Queue[ReceivedParcel] = asyncio.Queue()
        self._ack_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._tx_lock = asyncio.Lock()

    def clear_rx(self) -> None:
        """Drop any stale queued inbound parcels."""
        self._assembler.reset()
        while not self._rx_queue.empty():
            self._rx_queue.get_nowait()

    async def handle_notification(self, raw: bytes) -> None:
        """Dispatch a raw BLE notification on this characteristic."""
        data = bytes(raw)
        logger.debug("[%s] RX <= %s", self.name, data.hex())

        # Outbound-write ACKs: 00 00 01 XX or 00 00 03 XX
        if len(data) >= 4 and data[0] == 0x00 and data[1] == 0x00 and data[2] in (0x01, 0x03):
            await self._ack_queue.put(data)
            return

        ack_to_send, parcel = self._assembler.feed(data)
        if ack_to_send is not None:
            logger.debug("[%s] TX ACK => %s", self.name, ack_to_send.hex())
            try:
                await self._write_fn(ack_to_send)
            except Exception as exc:
                logger.debug("[%s] Failed sending ACK %s: %s", self.name, ack_to_send.hex(), exc)

        if parcel is not None:
            logger.debug(
                "[%s] RX parcel complete type=0x%02x len=%d",
                self.name,
                parcel.data_type,
                len(parcel.payload),
            )
            if self._on_parcel is not None:
                res = self._on_parcel(parcel)
                if asyncio.iscoroutine(res):
                    await res
            else:
                await self._rx_queue.put(parcel)

    async def receive(self, expected_type: int | None = None, timeout: float = 10.0) -> ReceivedParcel:
        """Wait for the next reassembled parcel (optionally filtering by data_type)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        buffered: list[ReceivedParcel] = []

        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"[{self.name}] Timed out waiting for parcel"
                        + (f" type=0x{expected_type:02x}" if expected_type is not None else "")
                    )
                parcel = await asyncio.wait_for(self._rx_queue.get(), timeout=remaining)
                if expected_type is None or parcel.data_type == expected_type:
                    return parcel
                buffered.append(parcel)
        finally:
            for item in buffered:
                await self._rx_queue.put(item)

    async def send(self, data_type: int, payload: bytes, *, single_pack: bool = False) -> None:
        """Transmit a parcel using MiBLE multi-pack (or single-pack if requested and small enough)."""
        async with self._tx_lock:
            # Flush stale ACKs from any previous timed-out exchange
            while not self._ack_queue.empty():
                self._ack_queue.get_nowait()

            if single_pack and len(payload) <= self.dmtu - 2:
                pkt = encode_single_pack(data_type, payload)
                logger.debug("[%s] TX single => %s", self.name, pkt.hex())
                await self._write_fn(pkt)
                ack = await self._wait_ack()
                if ack == ACK_SINGLE_SUCCESS:
                    return
                if ack == ACK_SINGLE_BUSY:
                    raise ParcelError(f"[{self.name}] Device busy on single-pack write")
                raise ParcelError(f"[{self.name}] Unexpected single-pack ACK: {ack.hex()}")

            frames = split_frames(payload, dmtu=self.dmtu)
            cmd = encode_cmd_start(data_type, len(frames))
            logger.debug("[%s] TX start => %s (%d frames)", self.name, cmd.hex(), len(frames))
            await self._write_fn(cmd)

            ack = await self._wait_ack()
            if ack != ACK_READY:
                raise ParcelError(f"[{self.name}] Expected READY (00000101), got {ack.hex()}")

            start_idx = 0
            retries = 0
            while True:
                for frame in frames[start_idx:]:
                    logger.debug("[%s] TX frame => %s", self.name, frame.hex())
                    await self._write_fn(frame)
                    # Small pacing delay avoids overflowing peripheral BLE RX buffers
                    await asyncio.sleep(0.008)

                ack = await self._wait_ack()
                if ack == ACK_SUCCESS:
                    return
                if ack.startswith(ACK_RETRANSMIT_PREFIX) and len(ack) >= 6:
                    retries += 1
                    if retries > 3:
                        raise ParcelError(f"[{self.name}] Exceeded max retransmit attempts")
                    req_seq = struct.unpack_from("<H", ack, 4)[0]
                    start_idx = max(0, req_seq - 1)
                    logger.warning("[%s] Device requested retransmit from seq=%d", self.name, req_seq)
                    continue
                if ack == ACK_BUSY:
                    raise ParcelError(f"[{self.name}] Multi-write failed: device busy")
                if ack == ACK_TIMEOUT:
                    raise ParcelError(f"[{self.name}] Multi-write failed: device timeout")
                if ack == ACK_CANCEL:
                    raise ParcelError(f"[{self.name}] Multi-write failed: canceled")
                raise ParcelError(f"[{self.name}] Unexpected completion ACK: {ack.hex()}")

    async def _wait_ack(self) -> bytes:
        try:
            return await asyncio.wait_for(self._ack_queue.get(), timeout=self.ack_timeout)
        except TimeoutError as exc:
            raise TimeoutError(f"[{self.name}] Timed out waiting for parcel ACK") from exc
