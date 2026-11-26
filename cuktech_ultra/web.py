"""Local FastAPI + WebSocket dashboard backend for CUKTECH 10 GaN Charger Ultra."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .ble_client import CuktechUltraClient
from .device import (
    ChargerState,
    ExtendedProtocolSwitches,
    LegacyProtocolSwitches,
    SceneMode,
    ScreenLanguage,
    ScreenTimeout,
)

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class ModeBody(BaseModel):
    mode: int = Field(..., ge=1, le=4)


class PortToggleBody(BaseModel):
    port: str
    enabled: bool


class TimerBody(BaseModel):
    port: str
    minutes: int = Field(..., ge=0, le=1440)


class ScreenBody(BaseModel):
    screen_save_time: int | None = None
    screen_language: int | None = None
    screenoff_while_idle: bool | None = None
    screen_direction_lock: bool | None = None


class UsbATrickleBody(BaseModel):
    enabled: bool


class ProtocolV1Body(BaseModel):
    scp: bool
    mipps: bool
    ufcs: bool


class ProtocolV2Body(BaseModel):
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


def create_app(client: CuktechUltraClient, *, auto_connect: bool = True) -> FastAPI:
    ws_clients: set[WebSocket] = set()
    poll_task: asyncio.Task[Any] | None = None

    async def broadcast_state(state: ChargerState) -> None:
        if not ws_clients:
            return
        payload = state.to_dict()
        dead: list[WebSocket] = []
        for ws in list(ws_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.discard(ws)

    async def _poll_loop() -> None:
        try:
            while True:
                await asyncio.sleep(3.0)
                if client.is_connected:
                    try:
                        await client.refresh_telemetry()
                    except Exception as exc:
                        logger.debug("Telemetry poll error: %s", exc)
        except asyncio.CancelledError:
            return

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal poll_task
        client.add_listener(broadcast_state)
        if auto_connect:
            for attempt in range(2):
                try:
                    await client.connect()
                    break
                except Exception as exc:
                    logger.error(
                        "Initial BLE connection attempt %d failed (%s): %s",
                        attempt + 1,
                        type(exc).__name__,
                        exc,
                    )
                    if attempt == 0:
                        await asyncio.sleep(1.0)
        poll_task = asyncio.create_task(_poll_loop())
        try:
            yield
        finally:
            if poll_task:
                poll_task.cancel()
            client.remove_listener(broadcast_state)
            await client.disconnect()

    app = FastAPI(title="CUKTECH 10 Ultra Local Controller", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "connected": client.is_connected, "mock": client.state.mock_mode}

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        return client.state.to_dict()

    @app.post("/api/connect")
    async def reconnect() -> dict[str, Any]:
        try:
            if client.is_connected:
                await client.refresh()
            else:
                await client.connect()
            return client.state.to_dict()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/refresh")
    async def refresh() -> dict[str, Any]:
        if not client.is_connected:
            raise HTTPException(status_code=503, detail="Charger not connected")
        await client.refresh()
        return client.state.to_dict()

    @app.post("/api/mode")
    async def set_mode(body: ModeBody) -> dict[str, Any]:
        if not client.is_connected:
            raise HTTPException(status_code=503, detail="Charger not connected")
        await client.set_scene_mode(SceneMode(body.mode))
        return client.state.to_dict()

    @app.post("/api/port")
    async def set_port(body: PortToggleBody) -> dict[str, Any]:
        if not client.is_connected:
            raise HTTPException(status_code=503, detail="Charger not connected")
        try:
            await client.set_port_enabled(body.port, body.enabled)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return client.state.to_dict()

    @app.post("/api/timer")
    async def set_timer(body: TimerBody) -> dict[str, Any]:
        if not client.is_connected:
            raise HTTPException(status_code=503, detail="Charger not connected")
        try:
            await client.set_port_timer(body.port, body.minutes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return client.state.to_dict()

    @app.post("/api/screen")
    async def set_screen(body: ScreenBody) -> dict[str, Any]:
        if not client.is_connected:
            raise HTTPException(status_code=503, detail="Charger not connected")
        if body.screen_save_time is not None:
            await client.set_screen_timeout(ScreenTimeout(body.screen_save_time))
        if body.screen_language is not None:
            await client.set_screen_language(ScreenLanguage(body.screen_language))
        if body.screenoff_while_idle is not None:
            await client.set_screenoff_while_idle(body.screenoff_while_idle)
        if body.screen_direction_lock is not None:
            await client.set_screen_direction_lock(body.screen_direction_lock)
        return client.state.to_dict()

    @app.post("/api/usb-a-trickle")
    async def set_usb_a_trickle(body: UsbATrickleBody) -> dict[str, Any]:
        if not client.is_connected:
            raise HTTPException(status_code=503, detail="Charger not connected")
        await client.set_usb_a_always_on(body.enabled)
        return client.state.to_dict()

    @app.post("/api/protocol/v1")
    async def set_protocol_v1(body: ProtocolV1Body) -> dict[str, Any]:
        if not client.is_connected:
            raise HTTPException(status_code=503, detail="Charger not connected")
        await client.set_protocol_switches_v1(
            LegacyProtocolSwitches(scp=body.scp, mipps=body.mipps, ufcs=body.ufcs)
        )
        return client.state.to_dict()

    @app.post("/api/protocol/v2")
    async def set_protocol_v2(body: ProtocolV2Body) -> dict[str, Any]:
        if not client.is_connected:
            raise HTTPException(status_code=503, detail="Charger not connected")
        await client.set_protocol_switches_v2(ExtendedProtocolSwitches(**body.model_dump()))
        return client.state.to_dict()

    @app.post("/api/reset")
    async def reset_charger() -> dict[str, Any]:
        if not client.is_connected:
            raise HTTPException(status_code=503, detail="Charger not connected")
        await client.reset_settings()
        return client.state.to_dict()

    @app.websocket("/ws")
    async def ws_telemetry(ws: WebSocket) -> None:
        await ws.accept()
        ws_clients.add(ws)
        try:
            await ws.send_json(client.state.to_dict())
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            ws_clients.discard(ws)

    return app
