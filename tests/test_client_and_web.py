"""End-to-end async client and FastAPI Web UI tests using MockChargerTransport."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuktech_ultra.ble_client import CuktechUltraClient
from cuktech_ultra.crypto import CryptoError
from cuktech_ultra.device import (
    ExtendedProtocolSwitches,
    SceneMode,
    ScreenLanguage,
    ScreenTimeout,
)
from cuktech_ultra.mock_device import MOCK_DEFAULT_TOKEN, MockChargerTransport
from cuktech_ultra.web import create_app


@pytest.mark.asyncio
async def test_full_ble_stack_e2e_with_mock_peripheral() -> None:
    transport = MockChargerTransport(token_hex=MOCK_DEFAULT_TOKEN, push_interval=0.2)
    client = CuktechUltraClient(transport, MOCK_DEFAULT_TOKEN)

    updates = []
    client.add_listener(lambda st: updates.append(st.total_power_w))

    state = await client.connect()
    try:
        assert client.is_connected is True
        assert state.model == "njcuk.fitting.ad1204"
        assert state.firmware_revision == "2.1.2_0045"
        assert state.ports["c1"].voltage_v == 20.0
        assert state.ports["c1"].protocol_name == "PD"
        assert state.ports["c1"].protocol_power_w == 65

        # Wait for unsolicited 001B properties_changed push
        await asyncio.sleep(0.35)
        assert len(updates) >= 2

        # Control scene mode
        await client.set_scene_mode(SceneMode.TURBO_SINGLE)
        assert client.state.scene_mode == int(SceneMode.TURBO_SINGLE)

        # Toggle port C2 off & verify telemetry reflects off
        await client.set_port_enabled("c2", False)
        assert client.state.ports["c2"].enabled is False
        await client.refresh_telemetry()
        assert client.state.ports["c2"].power_w == 0.0

        # Countdown timer on C1
        await client.set_port_timer("c1", 90)
        assert client.state.ports["c1"].delay_enabled is True
        assert client.state.ports["c1"].delay_minutes == 90

        # Screen settings & USB-A trickle
        await client.set_screen_timeout(ScreenTimeout.MIN_10)
        await client.set_screen_language(ScreenLanguage.ENGLISH)
        await client.set_screenoff_while_idle(False)
        await client.set_screen_direction_lock(True)
        await client.set_usb_a_always_on(True)
        assert client.state.screen_save_time == int(ScreenTimeout.MIN_10)
        assert client.state.screen_language == int(ScreenLanguage.ENGLISH)
        assert client.state.screenoff_while_idle is False
        assert client.state.screen_direction_lock is True
        assert client.state.usb_a_always_on is True

        # Extended protocol v2
        sw = ExtendedProtocolSwitches(c1_pd=True, c1_pps=False, c1_ufcs=True)
        await client.set_protocol_switches_v2(sw)
        assert client.state.protocol_v2.c1_pps is False

        # Reset action (2.1) restores defaults
        await client.reset_settings()
        assert client.state.scene_mode == int(SceneMode.AI_SMART)
        assert client.state.ports["c2"].enabled is True
    finally:
        await client.disconnect()
        assert client.is_connected is False


@pytest.mark.asyncio
async def test_wrong_token_rejected_by_standard_auth() -> None:
    transport = MockChargerTransport(token_hex=MOCK_DEFAULT_TOKEN, push_interval=0)
    wrong_client = CuktechUltraClient(transport, "ffeeddccbbaa998877665544")
    with pytest.raises(CryptoError):
        await wrong_client.connect()
    assert wrong_client.is_connected is False


def test_web_dashboard_api_and_websocket() -> None:
    transport = MockChargerTransport(token_hex=MOCK_DEFAULT_TOKEN, push_interval=0)
    client = CuktechUltraClient(transport, MOCK_DEFAULT_TOKEN)
    client.state.mock_mode = True
    app = create_app(client, auto_connect=True)

    with TestClient(app) as http:
        res = http.get("/")
        assert res.status_code == 200
        assert "CUKTECH 10 Ultra" in res.text

        health = http.get("/api/health").json()
        assert health["ok"] is True and health["connected"] is True

        st = http.post("/api/mode", json={"mode": 3}).json()
        assert st["scene_mode"] == 3

        st = http.post("/api/port", json={"port": "c3", "enabled": False}).json()
        assert st["ports"]["c3"]["enabled"] is False

        st = http.post("/api/timer", json={"port": "c1", "minutes": 60}).json()
        assert st["ports"]["c1"]["delay_minutes"] == 60

        st = http.post("/api/usb-a-trickle", json={"enabled": True}).json()
        assert st["usb_a_always_on"] is True

        with http.websocket_connect("/ws") as ws:
            initial = ws.receive_json()
            assert initial["connected"] is True
            assert initial["model"] == "njcuk.fitting.ad1204"


def test_web_dashboard_preserves_timer_dropdown_on_state_refresh() -> None:
    """Regression guard: periodic renderState updates must not recreate port timer <select> nodes."""
    html = (Path(__file__).resolve().parents[1] / "cuktech_ultra" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    match = re.search(r"<script>(.*)</script>", html, re.S)
    assert match is not None
    script_src = match.group(1)

    # Static guard: renderState must not rebuild portsGrid or protoGrid via innerHTML
    assert ".innerHTML" not in script_src

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not available for DOM runtime verification")

    transport = MockChargerTransport(token_hex=MOCK_DEFAULT_TOKEN, push_interval=0)
    client = CuktechUltraClient(transport, MOCK_DEFAULT_TOKEN)
    client.state.mock_mode = True
    asyncio.run(client.connect())
    try:
        state_initial = client.state.to_dict()
        state_refreshed = json.loads(json.dumps(state_initial))
        state_refreshed["total_power_w"] = 88.5
        state_refreshed["ports"]["c1"]["power_w"] = 68.5
        state_refreshed["ports"]["c1"]["voltage_v"] = 20.1
        state_refreshed["ports"]["c1"]["current_a"] = 3.4
        state_refreshed["ports"]["c1"]["delay_enabled"] = True
        state_refreshed["ports"]["c1"]["delay_minutes"] = 120
        state_refreshed["screen_save_time"] = 3

        state_custom_timer = json.loads(json.dumps(state_refreshed))
        state_custom_timer["ports"]["c1"]["delay_minutes"] = 90
    finally:
        asyncio.run(client.disconnect())

    harness = r"""
const vm = require("vm");
const assert = require("assert");

class ClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  toggle(name, force) {
    if (force === undefined) {
      if (this.set.has(name)) { this.set.delete(name); return false; }
      this.set.add(name); return true;
    }
    if (force) this.set.add(name); else this.set.delete(name);
    return Boolean(force);
  }
  contains(name) { return this.set.has(name); }
}

class TextNode {
  constructor(value) { this.nodeValue = String(value); }
}

class Element {
  constructor(tag, id = null) {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.classList = new ClassList(this);
    this._textContent = "";
    this._value = "";
    this.checked = false;
    this.type = "";
    this.valueWrites = 0;
  }
  get textContent() { return this._textContent; }
  set textContent(v) {
    this._textContent = String(v);
    if (v === "") this.children = [];
  }
  get value() { return this._value; }
  set value(v) {
    this.valueWrites += 1;
    this._value = String(v);
  }
  appendChild(child) {
    if (child && typeof child === "object") child.parentNode = this;
    this.children.push(child);
    if (this.tagName === "SELECT" && child.tagName === "OPTION" && this._value === "") {
      this._value = child.value;
    }
    return child;
  }
  append(...nodes) { nodes.forEach(n => this.appendChild(n)); }
  remove() {
    if (!this.parentNode) return;
    const idx = this.parentNode.children.indexOf(this);
    if (idx >= 0) this.parentNode.children.splice(idx, 1);
    this.parentNode = null;
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  dispatchEvent(type, ev = {}) {
    (this.listeners[type] || []).forEach(fn => fn(ev));
  }
  querySelector(sel) {
    if (sel === 'option[data-dynamic="1"]') {
      return this.children.find(c => c.tagName === "OPTION" && c.dataset && c.dataset.dynamic === "1") || null;
    }
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      for (const c of node.children || []) {
        if (sel === "select[data-port-timer]" && c.tagName === "SELECT" && c.dataset && c.dataset.portTimer) {
          out.push(c);
        } else if (sel === "button[data-proto-field]" && c.tagName === "BUTTON" && c.dataset && c.dataset.protoField) {
          out.push(c);
        } else if (sel === ".watt-readout" && c.className === "watt-readout") {
          out.push(c);
        }
        walk(c);
      }
    };
    walk(this);
    return out;
  }
}

const byId = {};
const register = (tag, id) => (byId[id] = new Element(tag, id));
[
  ["span", "connDot"], ["span", "connText"], ["div", "deviceMeta"],
  ["span", "totalPower"], ["div", "modeBadge"], ["span", "peakLabel"],
  ["path", "chartLine"], ["path", "chartArea"], ["div", "toast"],
  ["select", "screenTimeout"], ["select", "screenLang"],
  ["input", "usbATrickle"], ["input", "idleOff"], ["input", "dirLock"],
  ["section", "portsGrid"], ["div", "protoGrid"],
].forEach(([tag, id]) => register(tag, id));

for (const v of ["5", "1", "2", "3", "4"]) {
  const o = new Element("option"); o.value = v; byId.screenTimeout.appendChild(o);
}
for (const v of ["1", "0"]) {
  const o = new Element("option"); o.value = v; byId.screenLang.appendChild(o);
}

const modeBtns = [1, 2, 3, 4].map(m => {
  const b = new Element("button"); b.dataset.mode = String(m); return b;
});

const posted = [];
const sandbox = {
  window: {},
  location: { protocol: "http:", host: "127.0.0.1:8765" },
  document: {
    getElementById: (id) => byId[id],
    querySelectorAll: (sel) => (sel === ".mode-btn" ? modeBtns : []),
    createElement: (tag) => new Element(tag),
    createTextNode: (t) => new TextNode(t),
  },
  fetch: (url, opts) => {
    if (opts && opts.method === "POST") {
      posted.push({ url, body: JSON.parse(opts.body) });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(stateInitial) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve(stateInitial) });
  },
  WebSocket: class { constructor() {} },
  setTimeout: () => 1,
  clearTimeout: () => {},
  confirm: () => true,
  stateInitial: STATE_INITIAL,
  stateRefreshed: STATE_REFRESHED,
  stateCustomTimer: STATE_CUSTOM_TIMER,
};

vm.runInNewContext(SCRIPT_SRC, sandbox);

// 1. Initial render
sandbox.renderState(sandbox.stateInitial);
const selectsBefore = byId.portsGrid.querySelectorAll("select[data-port-timer]");
assert.strictEqual(selectsBefore.length, 4);
const c1Select = selectsBefore[0];
const protosBefore = byId.protoGrid.querySelectorAll("button[data-proto-field]");
assert.strictEqual(protosBefore.length, 10);
const c1ProtoBefore = protosBefore[0];
assert.strictEqual(c1Select.value, "0");

// 2. Simulate user opening timer dropdown (pointerdown) while telemetry refreshes arrive
c1Select.dispatchEvent("pointerdown");
const writesBeforeRefresh = c1Select.valueWrites;
sandbox.renderState(sandbox.stateRefreshed);

const selectsAfter = byId.portsGrid.querySelectorAll("select[data-port-timer]");
const protosAfter = byId.protoGrid.querySelectorAll("button[data-proto-field]");
assert.strictEqual(selectsAfter[0], c1Select, "timer <select> DOM node must be preserved across refreshes");
assert.strictEqual(protosAfter[0], c1ProtoBefore, "protocol button DOM node must be preserved");
assert.strictEqual(c1Select.valueWrites, writesBeforeRefresh, "must not overwrite select.value while dropdown is open");
assert.strictEqual(c1Select.value, "0");
const watts = byId.portsGrid.querySelectorAll(".watt-readout");
assert.strictEqual(watts[0].children[0].nodeValue, "68.5");

// 3. Blur (user dismisses dropdown without selecting) syncs back to latest committed state
c1Select.dispatchEvent("blur");
assert.strictEqual(c1Select.value, "120");

// 4. Non-preset countdown minutes create and clean up a dynamic option
sandbox.renderState(sandbox.stateCustomTimer);
assert.strictEqual(c1Select.value, "90");
const dynOpt = c1Select.querySelector('option[data-dynamic="1"]');
assert.ok(dynOpt && dynOpt.textContent === "90 分钟");

sandbox.renderState(sandbox.stateInitial);
assert.strictEqual(c1Select.value, "0");
assert.strictEqual(c1Select.querySelector('option[data-dynamic="1"]'), null);

// 5. User selects a new timer option -> clears interacting flag and posts /api/timer
c1Select.dispatchEvent("pointerdown");
c1Select.value = "60";
c1Select.dispatchEvent("change");
assert.strictEqual(c1Select.dataset.interacting, undefined);
assert.strictEqual(posted.length, 1);
assert.deepStrictEqual(posted[0], { url: "/api/timer", body: { port: "c1", minutes: 60 } });

// 6. Screen timeout <select> is also protected while open
byId.screenTimeout.dispatchEvent("pointerdown");
sandbox.renderState(sandbox.stateRefreshed);
assert.notStrictEqual(byId.screenTimeout.value, "3");
byId.screenTimeout.dispatchEvent("blur");
assert.strictEqual(byId.screenTimeout.value, "3");
"""

    runner = (
        harness.replace("STATE_INITIAL", json.dumps(state_initial))
        .replace("STATE_REFRESHED", json.dumps(state_refreshed))
        .replace("STATE_CUSTOM_TIMER", json.dumps(state_custom_timer))
        .replace("SCRIPT_SRC", json.dumps(script_src))
    )
    proc = subprocess.run([node_bin, "-e", runner], capture_output=True, text=True)
    assert proc.returncode == 0, f"DOM runtime check failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
