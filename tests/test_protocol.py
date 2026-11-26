"""Unit tests for crypto, parcel framing, MIoT-BLE Spec v2, and Cuktech 10 Ultra codecs."""

from __future__ import annotations

import struct

import pytest

from cuktech_ultra.crypto import (
    CryptoError,
    MscCipher,
    compute_app_confirm,
    compute_dev_confirm,
    derive_session_keys,
    parse_token_hex,
    verify_dev_confirm,
)
from cuktech_ultra.device import (
    PIID_C1_C2_PROTOCOL,
    PIID_PORT_CTRL,
    PIID_PORT_INFO_C1,
    PIID_PORT_INFO_C3,
    PIID_PROTOCOL_CTL_EXTEND,
    SIID_CHARGER,
    ChargerState,
    ExtendedProtocolSwitches,
    LegacyProtocolSwitches,
    decode_dual_protocol,
    decode_port_info,
    decode_port_mask,
    decode_protocol_ctl_extend,
    decode_protocol_ctrl_v1,
    encode_dual_protocol,
    encode_port_info,
    encode_port_mask,
    encode_protocol_ctl_extend,
    encode_protocol_ctrl_v1,
    parse_mibeacon_fe95,
    supports_spec_v2,
)
from cuktech_ultra.parcel import (
    ACK_READY,
    ACK_SINGLE_SUCCESS,
    ACK_SUCCESS,
    ParcelAssembler,
    encode_cmd_start,
    encode_single_pack,
    split_frames,
)
from cuktech_ultra.spec import (
    ActionArg,
    PropertyReadReq,
    PropertyValue,
    PropertyWriteReq,
    SpecOpcode,
    SpecValueType,
    decode_frame,
    encode_action,
    encode_get_properties,
    encode_protocol_exchange,
    encode_set_properties,
)


def test_standard_auth_hkdf_and_aes_ccm_roundtrip() -> None:
    token = parse_token_hex("00112233445566778899aabb")
    app_rand = bytes(range(16))
    dev_rand = bytes(range(16, 32))

    keys = derive_session_keys(token, app_rand, dev_rand)
    assert len(keys.dev_key) == 16
    assert len(keys.app_key) == 16
    assert len(keys.dev_iv) == 4
    assert len(keys.app_iv) == 4
    assert len(keys.extra) == 24

    dev_confirm = compute_dev_confirm(keys.dev_key, dev_rand, app_rand)
    assert verify_dev_confirm(keys.dev_key, dev_rand, app_rand, dev_confirm)
    assert not verify_dev_confirm(keys.dev_key, dev_rand, app_rand, b"\x00" * 32)
    assert len(compute_app_confirm(keys.app_key, app_rand, dev_rand)) == 32

    # Encrypt as app -> decrypt with swapped keys (simulating device side)
    app_cipher = MscCipher(keys, initial_app_counter=0)
    swapped_keys = type(keys)(
        dev_key=keys.app_key,
        app_key=keys.dev_key,
        dev_iv=keys.app_iv,
        app_iv=keys.dev_iv,
        extra=keys.extra,
    )
    dev_side = MscCipher(swapped_keys, initial_app_counter=0)

    plaintext = encode_protocol_exchange(tid=7, version=2)
    ct = app_cipher.encrypt_to_device(plaintext)
    assert ct[:2] == b"\x00\x00"  # counter=0 LE
    assert len(ct) == 2 + len(plaintext) + 4  # 2B counter + plaintext + 4B MIC
    assert dev_side.decrypt_from_device(ct) == plaintext

    # Second packet increments counter to 1
    ct2 = app_cipher.encrypt_to_device(b"hello-cuktech")
    assert ct2[:2] == b"\x01\x00"
    assert dev_side.decrypt_from_device(ct2) == b"hello-cuktech"

    # Tampered ciphertext must fail authentication tag
    tampered = ct2[:-1] + bytes([ct2[-1] ^ 0xFF])
    with pytest.raises(CryptoError):
        dev_side.decrypt_from_device(tampered)


def test_parcel_multipack_and_singlepack_assembly() -> None:
    payload = bytes(range(50))
    frames = split_frames(payload, dmtu=18)
    assert len(frames) == 3  # 18 + 18 + 14
    assert frames[0][:2] == b"\x01\x00"
    assert frames[2][:2] == b"\x03\x00"

    asm = ParcelAssembler()
    ack, done = asm.feed(encode_cmd_start(0x00, len(frames)))
    assert ack == ACK_READY and done is None

    for f in frames[:-1]:
        ack, done = asm.feed(f)
        assert ack is None and done is None

    ack, done = asm.feed(frames[-1])
    assert ack == ACK_SUCCESS
    assert done is not None
    assert done.data_type == 0x00
    assert done.payload == payload

    # Single-pack path
    ack, done = asm.feed(encode_single_pack(0x0B, b"short"))
    assert ack == ACK_SINGLE_SUCCESS
    assert done is not None and done.data_type == 0x0B and done.payload == b"short"


def test_miot_ble_spec_v2_encoders_and_decoders() -> None:
    # 0xF0 protocol_exchange
    ex = encode_protocol_exchange(tid=1, version=2)
    assert ex == bytes.fromhex("05200100f0")
    dec_ex = decode_frame(ex)
    assert dec_ex.version == 2 and dec_ex.tid == 1 and dec_ex.opcode == SpecOpcode.PROTOCOL_EXCHANGE

    # 0x02 get_properties
    gp = encode_get_properties(tid=2, props=[PropertyReadReq(2, 1), PropertyReadReq(2, 16)])
    assert gp == bytes.fromhex("0c2002000202020100021000")

    # 0x00 set_properties (enter=2)
    sp = encode_set_properties(
        tid=3,
        props=[PropertyWriteReq(2, 14, SpecValueType.UINT8, 2)],
    )
    # len = 5 + 1 + (1 + 2 + 2 + 1) = 12 = 0x0C; header = (2<<12)|12 = 0x200C -> 0c 20
    # type_len = (1<<12)|1 = 0x1001 -> 01 10
    assert sp == bytes.fromhex("0c2003000001020e00011002")

    # 0x03 get_properties-rsp with one OK uint32, one type=15 raw/null, and one failed (-4001)
    body = bytearray([3])
    body.extend(struct.pack("<BHhHI", 2, 1, 0, (int(SpecValueType.UINT32) << 12) | 4, 0xC8200701))
    body.extend(struct.pack("<BHhH", 2, 19, 0, (15 << 12) | 0))
    body.extend(struct.pack("<BHh", 2, 21, -4001))
    raw_rsp = struct.pack("<HHB", (2 << 12) | (5 + len(body)), 2, int(SpecOpcode.GET_PROPERTIES_RSP)) + bytes(body)
    dec_rsp = decode_frame(raw_rsp)
    assert len(dec_rsp.properties) == 3
    assert dec_rsp.properties[0].code == 0 and dec_rsp.properties[0].value == 0xC8200701
    assert dec_rsp.properties[1].code == 0 and dec_rsp.properties[1].value_type == SpecValueType.RAW
    assert dec_rsp.properties[2].code == -4001 and dec_rsp.properties[2].value is None

    # 0x04 properties_changed (no code field!)
    pc_body = struct.pack("<BBHHB", 1, 2, 5, (int(SpecValueType.UINT8) << 12) | 1, 3)
    raw_pc = struct.pack("<HHB", (2 << 12) | (5 + len(pc_body)), 0, int(SpecOpcode.PROPERTIES_CHANGED)) + pc_body
    dec_pc = decode_frame(raw_pc)
    assert dec_pc.opcode == SpecOpcode.PROPERTIES_CHANGED
    assert dec_pc.properties[0] == PropertyValue(siid=2, piid=5, code=0, value_type=SpecValueType.UINT8, value=3)

    # 0x05 action & 0x06 action-rsp
    act = encode_action(tid=4, siid=2, aiid=1, args=[ActionArg(1, SpecValueType.UINT8, 9)])
    assert act[:8] == bytes.fromhex("0d20040005020101")
    act_rsp_raw = struct.pack("<HHBhB", (2 << 12) | 8, 4, int(SpecOpcode.ACTION_RSP), 0, 0)
    dec_act = decode_frame(act_rsp_raw)
    assert dec_act.action_code == 0 and dec_act.action_out == []


def test_cuktech_10_ultra_bitfield_codecs() -> None:
    # 20.0V, 3.2A, protocol=7, status=1 -> 0xC8200701
    raw = encode_port_info(20.0, 3.2, 7, 1)
    assert raw == 0xC8200701
    info = decode_port_info(raw, "c1")
    assert info["voltage_v"] == 20.0
    assert info["current_a"] == 3.2
    assert info["power_w"] == 64.0
    assert info["protocol_raw"] == 7
    assert info["status"] == 1
    assert info["shared_c3_a"] is False

    # C3 + A merged 5V mode (status=0x11)
    c3_shared = decode_port_info(encode_port_info(5.0, 2.0, 1, 0x11), "c3")
    assert c3_shared["shared_c3_a"] is True

    # Protocol dual pack
    dual = encode_dual_protocol(7, 100, 10, 44)
    assert decode_dual_protocol(dual) == (("PD", 100), ("UFCS", 44))

    # Port mask
    mask = encode_port_mask(c1=True, c2=False, c3=True, a=True)
    assert mask == 0b1101
    assert decode_port_mask(mask) == {"c1": True, "c2": False, "c3": True, "a": True}

    # Legacy v1 protocol switches
    v1 = LegacyProtocolSwitches(scp=True, mipps=False, ufcs=True)
    assert encode_protocol_ctrl_v1(v1) == 0b101
    assert decode_protocol_ctrl_v1(0b101) == v1

    # Extended v2 protocol switches
    v2 = ExtendedProtocolSwitches(
        c1_pd=True, c1_pps=True, c1_ufcs=False,
        c2_pd=True, c2_pps=False, c2_ufcs=True,
        c3_scp=True, c3_ufcs=False,
        a_scp=False, a_ufcs=True,
    )
    enc_v2 = encode_protocol_ctl_extend(v2)
    assert (enc_v2 & 0xFF) == (0x08 | 0x02 | 0x01)
    assert ((enc_v2 >> 8) & 0xFF) == (0x08 | 0x04 | 0x01)
    assert ((enc_v2 >> 16) & 0xFF) == 0x02
    assert ((enc_v2 >> 24) & 0xFF) == 0x01
    assert decode_protocol_ctl_extend(enc_v2) == v2

    # Firmware spec v2 threshold
    assert supports_spec_v2("2.1.2_0040") is True
    assert supports_spec_v2("2.1.2_0045") is True
    assert supports_spec_v2("2.1.2_0039") is False
    assert supports_spec_v2("2.0.9_0099") is False

    # State aggregation
    st = ChargerState()
    st.apply_property(PropertyValue(SIID_CHARGER, PIID_PORT_INFO_C1, 0, SpecValueType.UINT32, raw))
    st.apply_property(PropertyValue(SIID_CHARGER, PIID_PORT_INFO_C3, 0, SpecValueType.UINT32, encode_port_info(5.0, 2.0, 1, 0x11)))
    st.apply_property(PropertyValue(SIID_CHARGER, PIID_C1_C2_PROTOCOL, 0, SpecValueType.UINT32, dual))
    st.apply_property(PropertyValue(SIID_CHARGER, PIID_PORT_CTRL, 0, SpecValueType.UINT8, mask))
    st.apply_property(PropertyValue(SIID_CHARGER, PIID_PROTOCOL_CTL_EXTEND, 0, SpecValueType.UINT32, enc_v2))
    assert st.total_power_w == 74.0
    assert st.ports["c1"].protocol_name == "PD" and st.ports["c1"].protocol_power_w == 100
    assert st.ports["c2"].enabled is False
    assert st.ports["c3"].shared_c3_a is True
    assert st.protocol_v2 == v2


def test_mibeacon_fe95_mac_extraction() -> None:
    # Frame ctrl 0x0058 (bit 4 = MAC present), pdid=0x1234, counter=7, reversed MAC AA:BB:CC:DD:EE:FF
    raw = struct.pack("<HHB", 0x0058, 0x1234, 7) + bytes.fromhex("ffeeddccbbaa")
    info = parse_mibeacon_fe95(raw)
    assert info is not None
    assert info.pdid == 0x1234
    assert info.mac == "AA:BB:CC:DD:EE:FF"
