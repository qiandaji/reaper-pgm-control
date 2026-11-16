"""python-osc UDP 收发网关：桥接 <-> REAPER（仅用 REAPER 内置 OSC 控制面）。"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from pythonosc import dispatcher, osc_server
from pythonosc.osc_message_builder import OscMessageBuilder

from .models import Settings

log = logging.getLogger("reaper-bridge.osc")


class OscGateway:
    """接收 REAPER 反馈并转发命令。反馈地址与 pgm-control.ReaperOSC 一致。"""

    HEARTBEAT_INTERVAL_S = 2.0
    HEARTBEAT_ADDRESS = "/device/track/follows/device"
    DEVICE_TRACK_COUNT = 64
    DEVICE_REGION_COUNT = 64
    # REAPER 内置动作「Control surface: Refresh all surfaces」，触发全量反馈重发。
    REFRESH_ALL_SURFACES_ACTION = 41743

    def __init__(self, settings: Settings):
        self._settings = settings
        self._cb: Any = None
        self._dispatcher = dispatcher.Dispatcher()
        self._dispatcher.set_default_handler(lambda *_: None)
        self._server: Optional[osc_server.BlockingOSCUDPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop = threading.Event()
        self._last_error: Optional[str] = None
        self._register_handlers()

    def bind(self, callbacks: Any) -> None:
        """绑定回调对象（StateMachine），收到 OSC 反馈时调用。"""
        self._cb = callbacks

    # ---------- 接收（REAPER -> 桥接） ----------
    def _register_handlers(self) -> None:
        d = self._dispatcher
        d.map("/time", self._on_time)
        d.map("/play", self._on_play)
        d.map("/stop", self._on_stop)
        d.map("/pause", self._on_pause)
        d.map("/track/*/name", self._on_track_name)
        d.map("/track/*/mute", self._on_track_mute)
        d.map("/region/*/name", self._on_region_name)
        d.map("/region/*/time", self._on_region_time)
        d.map("/region/*/length", self._on_region_length)

    @staticmethod
    def _wildcard_int(address: str, pos: int) -> Optional[int]:
        parts = address.split("/")
        try:
            return int(parts[pos])
        except (IndexError, ValueError):
            return None

    def _on_time(self, address: str, *args: Any) -> None:
        if self._cb and args:
            self._cb.on_time(float(args[0]))

    def _on_play(self, address: str, *args: Any) -> None:
        if self._cb:
            if args and float(args[0]) == 0.0:
                self._cb.on_stop()
            else:
                self._cb.on_play()

    def _on_stop(self, address: str, *args: Any) -> None:
        if self._cb:
            self._cb.on_stop()

    def _on_pause(self, address: str, *args: Any) -> None:
        if self._cb:
            self._cb.on_pause()

    def _on_track_name(self, address: str, *args: Any) -> None:
        num = self._wildcard_int(address, 2)
        if self._cb and num is not None and args:
            self._cb.on_track_name(num, str(args[0]))

    def _on_track_mute(self, address: str, *args: Any) -> None:
        num = self._wildcard_int(address, 2)
        if self._cb and num is not None and args:
            self._cb.on_track_mute(num, bool(args[0]))

    def _on_region_name(self, address: str, *args: Any) -> None:
        num = self._wildcard_int(address, 2)
        if self._cb and num is not None and args:
            self._cb.on_region_name(num, str(args[0]))

    def _on_region_time(self, address: str, *args: Any) -> None:
        num = self._wildcard_int(address, 2)
        if self._cb and num is not None and args:
            self._cb.on_region_time(num, float(args[0]))

    def _on_region_length(self, address: str, *args: Any) -> None:
        num = self._wildcard_int(address, 2)
        if self._cb and num is not None and args:
            self._cb.on_region_length(num, float(args[0]))

    # ---------- 发送（桥接 -> REAPER） ----------
    # 关键：必须从与接收端同一个 socket（绑定 osc_listen_port）发出，
    # 这样 REAPER 在「仅监听（接收）」模式下回发给「最后消息源」的反馈
    # 才会落在 9101 上，而不是随机临时端口。
    def _send(self, address: str, *args: Any) -> None:
        server = self._server
        if server is None:
            self._last_error = "网关未启动"
            log.warning("OSC 网关未启动，丢弃发送: %s", address)
            return
        builder = OscMessageBuilder(address=address)
        for a in args:
            builder.add_arg(a)
        msg = builder.build()
        try:
            server.socket.sendto(
                msg.dgram,
                (self._settings.osc_reaper_host, self._settings.osc_reaper_port),
            )
            self._last_error = None
        except Exception:
            self._last_error = f"发送失败 {address}: {type(server.socket).__name__}"
            log.exception("OSC 发送失败: %s", address)

    def _send_handshake(self) -> None:
        # 心跳让「仅监听（接收）」模式的 REAPER 学到回送地址；
        # 声明设备容量会促使 REAPER 重发当前轨道/region 列表。
        self._send(self.HEARTBEAT_ADDRESS)
        self._send("/device/track/count", self.DEVICE_TRACK_COUNT)
        self._send("/device/region/count", self.DEVICE_REGION_COUNT)

    def _socket_alive(self) -> bool:
        server = self._server
        try:
            return server is not None and server.socket is not None and server.socket.fileno() >= 0
        except (OSError, ValueError):
            return False

    def _rebind_if_needed(self) -> None:
        """socket 死后自动重建（现场自愈）。旧 socket 先关，再绑原端口。"""
        if self._socket_alive():
            return
        if self._server is not None:
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        try:
            self._server = osc_server.BlockingOSCUDPServer(
                (self._settings.osc_listen_host, self._settings.osc_listen_port),
                self._dispatcher,
            )
            self._thread = threading.Thread(
                target=self._server.serve_forever, name="osc-gateway", daemon=True
            )
            self._thread.start()
            self._last_error = None
            log.warning(
                "OSC 网关 socket 已重建：%s:%s",
                self._settings.osc_listen_host,
                self._settings.osc_listen_port,
            )
        except OSError as exc:
            self._last_error = f"重建失败: {exc}"
            log.error("OSC 网关重建失败: %s", exc)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.HEARTBEAT_INTERVAL_S):
            try:
                self._rebind_if_needed()
                self._send_handshake()
            except Exception:
                log.exception("心跳循环异常（已跳过本轮，下轮继续）")

    def _request_refresh(self) -> None:
        """启动时立即握手 + 触发 REAPER 重发全量反馈（重启桥接可自愈）。"""
        self._send_handshake()
        self._send("/action", self.REFRESH_ALL_SURFACES_ACTION)

    def play(self) -> None:
        self._send("/play")

    def stop(self) -> None:
        self._send("/stop")

    def pause(self) -> None:
        self._send("/pause")

    def set_time(self, seconds: float) -> None:
        self._send("/time", float(seconds))

    def set_track_mute(self, track_number: int, muted: bool) -> None:
        self._send(f"/track/{int(track_number)}/mute", 1 if muted else 0)

    def status(self) -> dict:
        return {
            "listening": self._socket_alive(),
            "listen_host": self._settings.osc_listen_host,
            "listen_port": self._settings.osc_listen_port,
            "reaper_host": self._settings.osc_reaper_host,
            "reaper_port": self._settings.osc_reaper_port,
            "last_error": self._last_error,
        }

    # ---------- 生命周期 ----------
    def start(self) -> None:
        self._server = osc_server.BlockingOSCUDPServer(
            (self._settings.osc_listen_host, self._settings.osc_listen_port),
            self._dispatcher,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="osc-gateway", daemon=True
        )
        self._thread.start()
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="osc-gateway-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()
        self._request_refresh()
        log.info(
            "OSC 网关监听 %s:%s，心跳每 %.1fs 发往 %s:%s",
            self._settings.osc_listen_host,
            self._settings.osc_listen_port,
            self.HEARTBEAT_INTERVAL_S,
            self._settings.osc_reaper_host,
            self._settings.osc_reaper_port,
        )

    def shutdown(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
