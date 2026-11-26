"""MIoT-BLE Spec v2 binary frame encoder & decoder."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class SpecOpcode(IntEnum):
    SET_PROPERTIES = 0x00
    SET_PROPERTIES_RSP = 0x01
    GET_PROPERTIES = 0x02
    GET_PROPERTIES_RSP = 0x03
    PROPERTIES_CHANGED = 0x04
    ACTION = 0x05
    ACTION_RSP = 0x06
    EVENT_OCCURED = 0x07
    PROTOCOL_EXCHANGE = 0xF0


class SpecValueType(IntEnum):
    BOOL = 0
    UINT8 = 1
    INT8 = 2
    UINT16 = 3
    INT16 = 4
    UINT32 = 5
    INT32 = 6
    UINT64 = 7
    INT64 = 8
    FLOAT = 9
    STRING = 10
    RAW = 15  # Unknown / reserved / null type skipped gracefully by length


_STRUCT_FORMATS: dict[SpecValueType, str] = {
    SpecValueType.BOOL: "<B",
    SpecValueType.UINT8: "<B",
    SpecValueType.INT8: "<b",
    SpecValueType.UINT16: "<H",
    SpecValueType.INT16: "<h",
    SpecValueType.UINT32: "<I",
    SpecValueType.INT32: "<i",
    SpecValueType.UINT64: "<Q",
    SpecValueType.INT64: "<q",
    SpecValueType.FLOAT: "<f",
}


class SpecProtocolError(Exception):
    """Raised on malformed MIoT-BLE Spec frames."""


@dataclass(frozen=True)
class PropertyReadReq:
    siid: int
    piid: int


@dataclass(frozen=True)
class PropertyWriteReq:
    siid: int
    piid: int
    value_type: SpecValueType
    value: Any


@dataclass(frozen=True)
class PropertyValue:
    siid: int
    piid: int
    code: int = 0
    value_type: SpecValueType | None = None
    value: Any = None


@dataclass(frozen=True)
class ActionArg:
    piid: int
    value_type: SpecValueType
    value: Any


@dataclass(frozen=True)
class SpecFrame:
    version: int
    tid: int
    opcode: SpecOpcode
    properties: list[PropertyValue] = field(default_factory=list)
    action_code: int | None = None
    action_out: list[ActionArg] = field(default_factory=list)
    event_siid: int | None = None
    event_eiid: int | None = None
    event_args: list[ActionArg] = field(default_factory=list)
    raw_payload: bytes = b""


def encode_value(value_type: SpecValueType, value: Any) -> bytes:
    """Encode a MIoT-BLE Spec typed value (little-endian)."""
    if value_type == SpecValueType.STRING:
        if isinstance(value, bytes):
            return value
        return str(value).encode("utf-8")
    if value_type == SpecValueType.BOOL:
        return struct.pack("<B", 1 if bool(value) else 0)
    fmt = _STRUCT_FORMATS.get(value_type)
    if fmt is None:
        raise SpecProtocolError(f"Unsupported value type: {value_type}")
    return struct.pack(fmt, value)


def decode_value(value_type: SpecValueType | int, raw: bytes) -> Any:
    """Decode a MIoT-BLE Spec typed value (little-endian)."""
    if value_type == SpecValueType.STRING:
        return raw.decode("utf-8", errors="replace")
    if value_type == SpecValueType.BOOL:
        if len(raw) < 1:
            raise SpecProtocolError("Empty bool payload")
        return raw[0] != 0
    if not isinstance(value_type, SpecValueType) or value_type == SpecValueType.RAW:
        return raw if raw else None
    fmt = _STRUCT_FORMATS.get(value_type)
    if fmt is None:
        return raw if raw else None
    expected = struct.calcsize(fmt)
    if len(raw) != expected:
        # Tolerate shorter unsigned integer payloads by zero-padding
        if value_type in (SpecValueType.UINT8, SpecValueType.UINT16, SpecValueType.UINT32, SpecValueType.UINT64) and len(raw) < expected:
            raw = raw + b"\x00" * (expected - len(raw))
        else:
            raise SpecProtocolError(
                f"Length mismatch for {value_type.name}: expected {expected}, got {len(raw)}"
            )
    return struct.unpack(fmt, raw)[0]


def _encode_type_and_len(value_type: SpecValueType, length: int) -> bytes:
    if length > 0x0FFF:
        raise SpecProtocolError(f"Value too large for 12-bit length field: {length}")
    word = ((int(value_type) & 0x0F) << 12) | (length & 0x0FFF)
    return struct.pack("<H", word)


def _decode_type_and_len(word: int) -> tuple[SpecValueType, int]:
    type_id = (word >> 12) & 0x0F
    length = word & 0x0FFF
    try:
        vtype = SpecValueType(type_id)
    except ValueError:
        vtype = SpecValueType.RAW
    return vtype, length


def _build_frame(version: int, tid: int, opcode: SpecOpcode, body: bytes) -> bytes:
    total_len = 5 + len(body)
    if total_len > 0x0FFF:
        raise SpecProtocolError(f"Spec frame too long: {total_len}")
    header = ((version & 0x0F) << 12) | (total_len & 0x0FFF)
    return struct.pack("<HHB", header, tid & 0xFFFF, int(opcode)) + body


def encode_protocol_exchange(tid: int, version: int = 2) -> bytes:
    """Encode opcode 0xF0 protocol_exchange request (5 bytes)."""
    return _build_frame(version=version, tid=tid, opcode=SpecOpcode.PROTOCOL_EXCHANGE, body=b"")


def encode_get_properties(
    tid: int,
    props: list[PropertyReadReq],
    *,
    version: int = 2,
) -> bytes:
    """Encode opcode 0x02 get_properties request."""
    if not (1 <= len(props) <= 255):
        raise SpecProtocolError(f"Invalid property count: {len(props)}")
    body = bytearray([len(props)])
    for prop in props:
        body.extend(struct.pack("<BH", prop.siid, prop.piid))
    return _build_frame(version=version, tid=tid, opcode=SpecOpcode.GET_PROPERTIES, body=bytes(body))


def encode_set_properties(
    tid: int,
    props: list[PropertyWriteReq],
    *,
    version: int = 2,
) -> bytes:
    """Encode opcode 0x00 set_properties request."""
    if not (1 <= len(props) <= 255):
        raise SpecProtocolError(f"Invalid property count: {len(props)}")
    body = bytearray([len(props)])
    for prop in props:
        encoded = encode_value(prop.value_type, prop.value)
        body.extend(struct.pack("<BH", prop.siid, prop.piid))
        body.extend(_encode_type_and_len(prop.value_type, len(encoded)))
        body.extend(encoded)
    return _build_frame(version=version, tid=tid, opcode=SpecOpcode.SET_PROPERTIES, body=bytes(body))


def encode_action(
    tid: int,
    siid: int,
    aiid: int,
    args: list[ActionArg] | None = None,
    *,
    version: int = 2,
) -> bytes:
    """Encode opcode 0x05 action request."""
    args = args or []
    body = bytearray(struct.pack("<BBB", siid, aiid, len(args)))
    for arg in args:
        encoded = encode_value(arg.value_type, arg.value)
        body.extend(struct.pack("<H", arg.piid))
        body.extend(_encode_type_and_len(arg.value_type, len(encoded)))
        body.extend(encoded)
    return _build_frame(version=version, tid=tid, opcode=SpecOpcode.ACTION, body=bytes(body))


def decode_frame(frame: bytes) -> SpecFrame:
    """Decode any inbound MIoT-BLE Spec v2 response or notification frame."""
    if len(frame) < 5:
        raise SpecProtocolError(f"Spec frame too short ({len(frame)} bytes): {frame.hex()}")

    header, tid, opcode_raw = struct.unpack_from("<HHB", frame, 0)
    version = (header >> 12) & 0x0F
    declared_len = header & 0x0FFF
    if declared_len > len(frame):
        raise SpecProtocolError(
            f"Truncated Spec frame: declared {declared_len} bytes, got {len(frame)}"
        )

    try:
        opcode = SpecOpcode(opcode_raw)
    except ValueError as exc:
        raise SpecProtocolError(f"Unknown MIoT-BLE Spec opcode: 0x{opcode_raw:02x}") from exc

    body = frame[5:declared_len]

    if opcode == SpecOpcode.PROTOCOL_EXCHANGE:
        return SpecFrame(version=version, tid=tid, opcode=opcode, raw_payload=body)

    if opcode == SpecOpcode.SET_PROPERTIES_RSP:
        if len(body) < 1:
            raise SpecProtocolError("Empty set_properties-rsp body")
        count = body[0]
        offset = 1
        props: list[PropertyValue] = []
        for _ in range(count):
            if offset + 5 > len(body):
                raise SpecProtocolError("Truncated set_properties-rsp entry")
            siid, piid, code = struct.unpack_from("<BHh", body, offset)
            offset += 5
            props.append(PropertyValue(siid=siid, piid=piid, code=code))
        return SpecFrame(version=version, tid=tid, opcode=opcode, properties=props, raw_payload=body)

    if opcode == SpecOpcode.GET_PROPERTIES_RSP:
        if len(body) < 1:
            raise SpecProtocolError("Empty get_properties-rsp body")
        count = body[0]
        offset = 1
        props = []
        for _ in range(count):
            if offset + 5 > len(body):
                raise SpecProtocolError("Truncated get_properties-rsp entry header")
            siid, piid, code = struct.unpack_from("<BHh", body, offset)
            offset += 5
            if code != 0:
                props.append(PropertyValue(siid=siid, piid=piid, code=code))
                continue
            if offset + 2 > len(body):
                raise SpecProtocolError("Truncated get_properties-rsp type_len")
            type_len = struct.unpack_from("<H", body, offset)[0]
            offset += 2
            vtype, vlen = _decode_type_and_len(type_len)
            if offset + vlen > len(body):
                raise SpecProtocolError("Truncated get_properties-rsp value bytes")
            val = decode_value(vtype, body[offset : offset + vlen])
            offset += vlen
            props.append(PropertyValue(siid=siid, piid=piid, code=0, value_type=vtype, value=val))
        return SpecFrame(version=version, tid=tid, opcode=opcode, properties=props, raw_payload=body)

    if opcode == SpecOpcode.PROPERTIES_CHANGED:
        if len(body) < 1:
            raise SpecProtocolError("Empty properties_changed body")
        count = body[0]
        offset = 1
        props = []
        for _ in range(count):
            if offset + 5 > len(body):
                raise SpecProtocolError("Truncated properties_changed entry header")
            siid, piid, type_len = struct.unpack_from("<BHH", body, offset)
            offset += 5
            vtype, vlen = _decode_type_and_len(type_len)
            if offset + vlen > len(body):
                raise SpecProtocolError("Truncated properties_changed value bytes")
            val = decode_value(vtype, body[offset : offset + vlen])
            offset += vlen
            props.append(PropertyValue(siid=siid, piid=piid, code=0, value_type=vtype, value=val))
        return SpecFrame(version=version, tid=tid, opcode=opcode, properties=props, raw_payload=body)

    if opcode == SpecOpcode.ACTION_RSP:
        if len(body) < 2:
            raise SpecProtocolError("Truncated action-rsp code")
        code = struct.unpack_from("<h", body, 0)[0]
        out_args: list[ActionArg] = []
        if code == 0 and len(body) >= 3:
            count = body[2]
            offset = 3
            for _ in range(count):
                if offset + 4 > len(body):
                    raise SpecProtocolError("Truncated action-rsp output entry")
                piid, type_len = struct.unpack_from("<HH", body, offset)
                offset += 4
                vtype, vlen = _decode_type_and_len(type_len)
                if offset + vlen > len(body):
                    raise SpecProtocolError("Truncated action-rsp value bytes")
                val = decode_value(vtype, body[offset : offset + vlen])
                offset += vlen
                out_args.append(ActionArg(piid=piid, value_type=vtype, value=val))
        return SpecFrame(
            version=version,
            tid=tid,
            opcode=opcode,
            action_code=code,
            action_out=out_args,
            raw_payload=body,
        )

    if opcode == SpecOpcode.EVENT_OCCURED:
        if len(body) < 3:
            raise SpecProtocolError("Truncated event_occured header")
        siid, eiid, count = struct.unpack_from("<BBB", body, 0)
        offset = 3
        ev_args: list[ActionArg] = []
        for _ in range(count):
            if offset + 4 > len(body):
                raise SpecProtocolError("Truncated event_occured arg header")
            piid, type_len = struct.unpack_from("<HH", body, offset)
            offset += 4
            vtype, vlen = _decode_type_and_len(type_len)
            if offset + vlen > len(body):
                raise SpecProtocolError("Truncated event_occured arg value")
            val = decode_value(vtype, body[offset : offset + vlen])
            offset += vlen
            ev_args.append(ActionArg(piid=piid, value_type=vtype, value=val))
        return SpecFrame(
            version=version,
            tid=tid,
            opcode=opcode,
            event_siid=siid,
            event_eiid=eiid,
            event_args=ev_args,
            raw_payload=body,
        )

    return SpecFrame(version=version, tid=tid, opcode=opcode, raw_payload=body)
