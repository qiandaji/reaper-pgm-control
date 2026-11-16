"""FastAPI 入口：REST + WS + 静态前端 + 生命周期（OSC 网关与 state 广播）。"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .models import (Settings, VERSION, Defaults, SongSettings, StateSnapshot,
                     get_settings)
from .osc_gateway import OscGateway
from .state_machine import StateMachine
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    force=True,
)

log = logging.getLogger("reaper-bridge.main")


def _r(region: dict, settings: dict) -> dict:
    return {
        "id": region["id"],
        "name": region["name"],
        "index": region["index"],
        "start_s": region["start_s"],
        "end_s": region["end_s"],
        "length_s": region["length_s"],
        "limit_s": settings.get("limit_s"),
        "end_action": settings.get("end_action") or "stop_mute",
        "mute_tracks": list(settings.get("mute_tracks") or []),
    }


class AutoNextBody(BaseModel):
    enabled: bool


class TransportBody(BaseModel):
    action: str
    region_id: str | None = None
    autoplay: bool | None = None


class SetDefaultsBody(BaseModel):
    limit_s: float | None = None
    end_action: str | None = None
    mute_tracks: list[int] | None = None
    play_on_switch: bool | None = None


class SetTrackMuteBody(BaseModel):
    index: int
    muted: bool


def create_app(settings: Settings | None = None, state_machine: StateMachine | None = None,
               osc_gateway: OscGateway | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    sm = state_machine
    gateway = osc_gateway
    if sm is None:
        gateway = gateway or OscGateway(settings)
        sm = StateMachine(Store(settings.data_dir), gateway)
    elif gateway is None:
        gateway = getattr(sm, "_gateway", None)

    connections: set[WebSocket] = set()
    conn_lock = asyncio.Lock()
    send_lock = asyncio.Lock()

    async def safe_send(ws: WebSocket, payload: dict) -> None:
        async with send_lock:
            await ws.send_json(payload)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_dirs()
        if gateway is not None:
            gateway.bind(sm)
            gateway.start()
        sm._gateway = gateway
        task: asyncio.Task | None = None

        async def broadcast() -> None:
            while True:
                sm.evaluate()
                payload = sm.snapshot()
                async with conn_lock:
                    targets = list(connections)
                for ws in targets:
                    try:
                        await safe_send(ws, payload)
                    except Exception:
                        async with conn_lock:
                            connections.discard(ws)
                await asyncio.sleep(settings.broadcast_interval_s)

        task = asyncio.create_task(broadcast())
        log.info("PGM Reaper 控制端启动: HTTP/WS 0.0.0.0:%s", settings.rpc_port)
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if gateway is not None:
                gateway.shutdown()

    app = FastAPI(title="PGM Reaper Control", version=VERSION, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------- REST ----------
    @app.get("/api/health")
    def health() -> dict:
        osc = gateway.status() if gateway is not None else None
        return {"status": "ok", "version": VERSION, "osc": osc}

    @app.get("/api/state")
    def state() -> dict:
        return sm.snapshot()

    @app.get("/api/regions")
    def regions() -> dict:
        return {"regions": sm.regions_with_settings()}

    @app.put("/api/defaults")
    def put_defaults(body: SetDefaultsBody) -> dict:
        d = sm.set_defaults(body.limit_s, body.end_action, body.mute_tracks,
                            body.play_on_switch)
        return {"defaults": d}

    @app.put("/api/songs/{region_id}/settings")
    def put_song_settings(region_id: str, body: dict[str, Any]) -> dict:
        try:
            SongSettings.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors())
        try:
            saved = sm.set_song_settings(region_id, body)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"未知 region: {region_id}")
        return saved

    @app.delete("/api/songs/{region_id}/settings")
    def delete_song_settings(region_id: str) -> dict:
        try:
            deleted = sm.clear_song_settings(region_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"未知 region: {region_id}")
        return {"deleted": deleted}

    @app.put("/api/auto_next")
    def put_auto_next(body: AutoNextBody) -> dict:
        return {"auto_next": sm.set_auto_next(body.enabled)}

    @app.post("/api/transport")
    def transport(body: TransportBody) -> dict:
        action = body.action
        if action == "play_song":
            if not body.region_id:
                raise HTTPException(status_code=422, detail="play_song 需要 region_id")
            try:
                cur = sm.play_song(body.region_id, True if body.autoplay is None else bool(body.autoplay))
            except KeyError:
                raise HTTPException(status_code=404, detail=f"未知 region: {body.region_id}")
            return {"current_song": cur}
        if action == "stop":
            sm.stop()
            return {"stopped": True}
        if action == "toggle_play":
            sm.toggle_play()
            snap = sm.snapshot()
            return {"playing": snap["transport"]["playing"]}
        raise HTTPException(status_code=422, detail=f"未知 action: {action}")

    # ---------- WS ----------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        async with conn_lock:
            connections.add(ws)
        try:
            await safe_send(ws, sm.snapshot())
            while True:
                raw = await ws.receive_json()
                cmd = raw.get("cmd")
                if cmd == "ping":
                    await safe_send(ws, {"type": "pong"})
                    continue
                try:
                    ok, error = _handle_ws_command(sm, raw)
                    await safe_send(ws, {
                        "type": "ack", "ok": ok, "cmd": cmd,
                        "error": None if ok else error,
                    })
                except HTTPException as exc:
                    await safe_send(ws, {
                        "type": "ack", "ok": False, "cmd": cmd,
                        "error": exc.detail,
                    })
                except Exception as exc:  # noqa: BLE001
                    await safe_send(ws, {
                        "type": "ack", "ok": False, "cmd": cmd,
                        "error": str(exc),
                    })
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            async with conn_lock:
                connections.discard(ws)

    def _handle_ws_command(sm: StateMachine, raw: dict) -> tuple[bool, str | None]:
        cmd = raw.get("cmd")
        if cmd == "play_song":
            region_id = raw.get("region_id")
            if not region_id:
                return False, "play_song 需要 region_id"
            try:
                autoplay = raw.get("autoplay", True)
                sm.play_song(str(region_id), True if autoplay is None else bool(autoplay))
            except KeyError:
                return False, f"未知 region: {region_id}"
            return True, None
        if cmd == "stop":
            sm.stop()
            return True, None
        if cmd == "toggle_play":
            sm.toggle_play()
            return True, None
        if cmd == "set_song_settings":
            region_id = raw.get("region_id")
            settings = raw.get("settings") or {}
            if not region_id:
                return False, "set_song_settings 需要 region_id"
            try:
                SongSettings.model_validate(settings)
            except ValidationError as exc:
                return False, json.dumps(exc.errors(), ensure_ascii=False)
            try:
                sm.set_song_settings(str(region_id), settings)
            except KeyError:
                return False, f"未知 region: {region_id}"
            return True, None
        if cmd == "clear_song_settings":
            region_id = raw.get("region_id")
            if not region_id:
                return False, "clear_song_settings 需要 region_id"
            try:
                sm.clear_song_settings(str(region_id))
            except KeyError:
                return False, f"未知 region: {region_id}"
            return True, None
        if cmd == "set_defaults":
            body = raw.get("settings") or raw.get("defaults") or {}
            try:
                sm.set_defaults(body.get("limit_s"), body.get("end_action"),
                                body.get("mute_tracks"), body.get("play_on_switch"))
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)
            return True, None
        if cmd == "set_auto_next":
            sm.set_auto_next(bool(raw.get("enabled")))
            return True, None
        if cmd == "set_track_mute":
            index = raw.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 1:
                return False, "set_track_mute 需要 index>=1 的整数"
            sm.set_track_mute(index, bool(raw.get("muted")))
            return True, None
        return False, f"未知 cmd: {cmd}"

    # ---------- 静态前端（前端子代理所有，本服务只 mount） ----------
    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=61370, reload=False)