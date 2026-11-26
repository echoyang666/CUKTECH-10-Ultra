"""One-time Xiaomi Cloud QR-code login helper to retrieve the charger's BLE token & MAC.

Once the device token and MAC are saved to ~/.cuktech-ultra/config.json, the client runs
100% offline over local BLE without Xiaomi Cloud or the Mi Home app.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import string
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .device import TARGET_MODELS

USER_AGENT = "Android-7.1.1-1.0.0-ONEPLUS A3010-136-7891234567890 APP/xiaomi.smarthome APPV/62830"
API_SERVERS = {
    "cn": "https://api.io.mi.com/app",
    "de": "https://de.api.io.mi.com/app",
    "us": "https://us.api.io.mi.com/app",
    "ru": "https://ru.api.io.mi.com/app",
    "sg": "https://sg.api.io.mi.com/app",
    "i2": "https://i2.api.io.mi.com/app",
}


class CloudAuthError(Exception):
    """Raised on Xiaomi Cloud QR login or device listing errors."""


@dataclass
class CloudDeviceInfo:
    did: str
    name: str
    model: str
    mac: str
    token: str
    beaconkey: str = ""

    @property
    def best_ble_token(self) -> str:
        """Prefer device token (StandardAuth loginToken from /home/device_list) over MiBeacon beaconkey."""
        return self.token or self.beaconkey


def _strip_xiaomi_json_prefix(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("&&&START&&&"):
        cleaned = cleaned[len("&&&START&&&") :]
    return json.loads(cleaned)


def _signed_nonce(ssecurity: str, nonce: str) -> str:
    h = hashlib.sha256()
    h.update(base64.b64decode(ssecurity))
    h.update(base64.b64decode(nonce))
    return base64.b64encode(h.digest()).decode("ascii")


def _generate_nonce() -> str:
    millis = int(time.time() * 1000) // 60000
    raw = os.urandom(8) + millis.to_bytes(4, "big")
    return base64.b64encode(raw).decode("ascii")


def _rc4_crypt(key: bytes, data: bytes) -> bytes:
    # MiOT API uses RC4 with 1024-byte keystream drop
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]

    i = j = 0
    for _ in range(1024):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]

    out = bytearray(len(data))
    for k, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[k] = b ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def _generate_enc_signature(uri: str, method: str, signed_nonce: str, params: dict[str, str]) -> str:
    parts = [method.upper(), uri.split("/app", 1)[-1]]
    for k in sorted(params.keys()):
        parts.append(f"{k}={params[k]}")
    parts.append(signed_nonce)
    raw = "&".join(parts).encode("utf-8")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


def _generate_enc_params(uri: str, method: str, signed_nonce: str, nonce: str, params: dict[str, str]) -> dict[str, str]:
    key = base64.b64decode(signed_nonce)
    p = dict(params)
    p["rc4_hash__"] = _generate_enc_signature(uri, method, signed_nonce, p)
    enc: dict[str, str] = {}
    for k, v in p.items():
        enc[k] = base64.b64encode(_rc4_crypt(key, v.encode("utf-8"))).decode("ascii")
    enc["signature"] = _generate_enc_signature(uri, method, signed_nonce, enc)
    enc["ssecurity"] = ""
    enc["_nonce"] = nonce
    return enc


class XiaomiCloudQRClient:
    """Interactive Xiaomi account QR login client for one-time BLE key retrieval."""

    def __init__(self, server: str = "cn") -> None:
        if server not in API_SERVERS:
            raise ValueError(f"Unsupported server '{server}'. Choose from: {', '.join(API_SERVERS)}")
        self.server = server
        self._device_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=16))
        self._http = httpx.Client(
            timeout=20.0,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._user_id = ""
        self._service_token = ""
        self._ssecurity = ""

    def close(self) -> None:
        self._http.close()

    def request_qr_session(self) -> dict[str, Any]:
        """Request a QR login URL and long-polling endpoint from account.xiaomi.com."""
        params = {
            "_qrsize": "240",
            "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "callback": "https://sts.api.io.mi.com/sts",
            "_hasLogo": "false",
            "sid": "xiaomiio",
            "serviceParam": "",
            "_locale": "zh_CN",
            "_dc": str(int(time.time() * 1000)),
        }
        resp = self._http.get("https://account.xiaomi.com/longPolling/loginUrl", params=params)
        resp.raise_for_status()
        data = _strip_xiaomi_json_prefix(resp.text)
        if data.get("code") != 0:
            raise CloudAuthError(f"Failed to start QR login: {data}")
        return data

    def wait_for_qr_confirmation(self, lp_url: str, timeout: float = 180.0) -> None:
        """Poll until the user scans and approves login in the Mi Home / Xiaomi app."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = self._http.get(lp_url, timeout=30.0)
            except httpx.ReadTimeout:
                continue
            if resp.status_code != 200:
                continue
            data = _strip_xiaomi_json_prefix(resp.text)
            if data.get("code") == 0 and data.get("location"):
                self._user_id = str(data.get("userId", ""))
                self._ssecurity = str(data.get("ssecurity", ""))
                loc_resp = self._http.get(data["location"])
                loc_resp.raise_for_status()
                token = self._http.cookies.get("serviceToken") or loc_resp.cookies.get("serviceToken")
                if not token:
                    raise CloudAuthError("QR login succeeded but serviceToken cookie was missing")
                self._service_token = token
                return
        raise CloudAuthError("Timed out waiting for QR code scan confirmation")

    def _miot_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        base = API_SERVERS[self.server]
        url = f"{base}{path}"
        nonce = _generate_nonce()
        snonce = _signed_nonce(self._ssecurity, nonce)
        enc_params = _generate_enc_params(
            url,
            "POST",
            snonce,
            nonce,
            {"data": json.dumps(payload, separators=(",", ":"))},
        )
        headers = {
            "User-Agent": USER_AGENT,
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
            "Accept-Encoding": "identity",
        }
        cookies = {
            "userId": self._user_id,
            "yetAnotherServiceToken": self._service_token,
            "serviceToken": self._service_token,
            "locale": "zh_CN",
            "timezone": "GMT+08:00",
            "is_daylight": "0",
            "dst_offset": "0",
            "channel": "MI_APP_STORE",
        }
        resp = self._http.post(url, data=enc_params, headers=headers, cookies=cookies)
        resp.raise_for_status()
        decrypted = _rc4_crypt(base64.b64decode(snonce), base64.b64decode(resp.text))
        return json.loads(decrypted.decode("utf-8"))

    def list_devices(self, only_cuktech: bool = True) -> list[CloudDeviceInfo]:
        """List devices on the Xiaomi account and fetch BLE beaconkey/token for matching chargers."""
        res = self._miot_post("/home/device_list", {"getVirtualModel": False, "getHuamiDevices": 0})
        raw_list = (res.get("result") or {}).get("list") or []
        devices: list[CloudDeviceInfo] = []
        for item in raw_list:
            model = str(item.get("model", ""))
            if only_cuktech and model not in TARGET_MODELS and "njcuk" not in model.lower():
                continue
            did = str(item.get("did", ""))
            beaconkey = ""
            try:
                bk_res = self._miot_post("/v2/device/blt_get_beaconkey", {"did": did, "pdid": 1})
                beaconkey = str((bk_res.get("result") or {}).get("beaconkey", ""))
            except Exception:
                pass
            devices.append(
                CloudDeviceInfo(
                    did=did,
                    name=str(item.get("name", model)),
                    model=model,
                    mac=str(item.get("mac", "")).upper(),
                    token=str(item.get("token", "")),
                    beaconkey=beaconkey,
                )
            )
        return devices
