"""桥接服务 pytest：状态机 / REST / WS / python-osc 编解码。"""
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient
from pythonosc.osc_message_builder import OscMessageBuilder

from app.main import create_app
from app.models import Settings, SongSettings, hhmmss, hhmmss_countdown
from app.osc_gateway import OscGateway
from app.state_machine import StateMachine
from app.store import Store


class FakeOscGateway:
    """假 OSC 网关：记录调用序列，供状态机单元测试。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def bind(self, cb):
        self.cb = cb

    def start(self):
        self.started = True

    def shutdown(self):
        self.stopped = True

    def status(self):
        return {"listening": False, "listen_host": "", "listen_port": 9101,
                "reaper_host": "127.0.0.1", "reaper_port": 9100, "last_error": None}

    def _rec(self, name, *args):
        self.calls.append((name, *args))

    def play(self):
        self._rec("play")

    def stop(self):
        self._rec("stop")

    def pause(self):
        self._rec("pause")

    def set_time(self, seconds):
        self._rec("set_time", float(seconds))

    def set_track_mute(self, track_number, muted):
        self._rec("set_track_mute", int(track_number), bool(muted))


def make_sm(tmp_path, regions=None):
    settings = Settings(data_dir=tmp_path / "data", osc_reaper_port=9100, osc_listen_port=9101)
    store = Store(settings.data_dir)
    fake = FakeOscGateway()
    sm = StateMachine(store, fake)
    for rid, name, start, end in (regions or [("region:0", "A", 0.0, 30.0), ("region:1", "B", 30.0, 60.0)]):
        sm.add_region(rid, name, start, end)
    return sm, store, fake


def find_call(gw, *args):
    for i, c in enumerate(gw.calls):
        if c == args:
            return i
    return -1


# ---------- 状态机 ----------

def test_effective_settings_song_overrides_defaults(tmp_path):
    sm, store, _ = make_sm(tmp_path)
    sm.set_defaults(10.0, "stop", [1])
    sm.set_song_settings("region:0", SongSettings(limit_s=5.0, end_action="none", mute_tracks=[2]).model_dump(exclude_unset=True))
    with sm._lock:
        s = sm._effective_settings_locked(sm._find_region_locked("region:0"))
    assert s["limit_s"] == 5.0
    assert s["end_action"] == "none"
    assert s["mute_tracks"] == [2]
    # region:1 只有 defaults
    with sm._lock:
        s = sm._effective_settings_locked(sm._find_region_locked("region:1"))
    assert s["limit_s"] == 10.0
    assert s["end_action"] == "stop"
    assert s["mute_tracks"] == [1]


def test_end_action_none_does_nothing(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_song_settings("region:0", {"end_action": "none", "limit_s": 1.0, "mute_tracks": [3]})
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek 完成，解除位置防抖
    gw.calls.clear()
    sm.on_time(1.0)  # 达 limit
    assert gw.calls == []
    assert sm.snapshot()["current_song"]["id"] == "region:0"


def test_end_action_stop_sends_stop_no_mute(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_song_settings("region:0", {"end_action": "stop", "limit_s": 1.0, "mute_tracks": [3]})
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek 完成
    gw.calls.clear()
    sm.on_time(1.0)
    assert ("stop",) in gw.calls
    assert find_call(gw, "set_track_mute", 3, True) == -1
    assert sm.snapshot()["transport"]["playing"] is False


def test_end_action_stop_mute_mutes_and_restore_on_next(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_song_settings("region:0", {"end_action": "stop_mute", "limit_s": 1.0, "mute_tracks": [3, 5]})
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek 完成
    gw.calls.clear()
    sm.on_time(1.0)
    assert ("stop",) in gw.calls
    assert ("set_track_mute", 3, True) in gw.calls
    assert ("set_track_mute", 5, True) in gw.calls
    assert sm.snapshot()["transport"]["playing"] is False

    # 切歌恢复：再 play_song 时先 unmute 3/5 再跳转播放
    gw.calls.clear()
    sm.play_song("region:1")
    idx_mute_off_3 = find_call(gw, "set_track_mute", 3, False)
    idx_mute_off_5 = find_call(gw, "set_track_mute", 5, False)
    idx_play = find_call(gw, "play")
    assert idx_mute_off_3 != -1 and idx_mute_off_5 != -1
    assert idx_play > idx_mute_off_3 and idx_play > idx_mute_off_5
    assert sm.snapshot()["current_song"]["id"] == "region:1"


def test_limit_fires_once_not_every_evaluate(tmp_path):
    """到时动作只触发一次：position 停在 limit 后不再重复 stop/静音。"""
    sm, _, gw = make_sm(tmp_path)
    sm.set_song_settings("region:0", {"end_action": "stop_mute", "limit_s": 1.0, "mute_tracks": [3]})
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek 完成
    gw.calls.clear()
    sm.on_time(1.0)
    assert gw.calls.count(("stop",)) == 1
    assert gw.calls.count(("set_track_mute", 3, True)) == 1
    gw.calls.clear()
    sm.on_time(1.5)  # 仍停在 limit 之后
    assert ("stop",) not in gw.calls
    assert ("set_track_mute", 3, True) not in gw.calls
    assert sm.snapshot()["transport"]["playing"] is False


def test_toggle_play_after_limit_restarts_song(tmp_path):
    """limit 到点停过后再按播放 = 从头重播本曲（恢复静音并重新计时）。"""
    sm, _, gw = make_sm(tmp_path)
    sm.set_song_settings("region:0", {"end_action": "stop_mute", "limit_s": 1.0, "mute_tracks": [3]})
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek 完成
    sm.on_time(1.0)  # 到时 stop_mute
    assert sm.snapshot()["transport"]["playing"] is False
    gw.calls.clear()
    sm.toggle_play()
    assert ("set_track_mute", 3, False) in gw.calls  # 恢复被静音轨道
    assert ("set_time", 0.0) in gw.calls              # 回到本曲起点
    assert ("play",) in gw.calls
    assert sm.snapshot()["transport"]["playing"] is True
    assert sm.snapshot()["current_song"]["id"] == "region:0"
    gw.calls.clear()
    sm.on_time(0.5)  # 重新计时：0.5s 未到点
    assert ("stop",) not in gw.calls
    sm.on_time(1.2)  # 再次到点，正常触发
    assert ("stop",) in gw.calls
    assert sm.snapshot()["transport"]["playing"] is False


def test_osc_gateway_status_not_listening_before_start(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", osc_reaper_port=9100, osc_listen_port=9101)
    gw = OscGateway(settings)
    st = gw.status()
    assert st["listening"] is False
    assert st["listen_port"] == 9101
    assert st["reaper_port"] == 9100


def test_auto_next_cycle_loops_to_first(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_auto_next(True)
    for rid in ("region:0", "region:1"):
        sm.set_song_settings(rid, {"end_action": "stop_mute", "limit_s": 1.0, "mute_tracks": [3]})
    sm.play_song("region:0")
    assert sm.snapshot()["current_song"]["id"] == "region:0"
    sm.on_time(0.0)  # 确认 seek 完成
    sm.on_time(1.0)  # 达 limit -> auto_next 直接下一首（不做 stop_mute 静音）
    assert sm.snapshot()["current_song"]["id"] == "region:1"
    assert find_call(gw, "set_track_mute", 3, True) == -1
    gw.calls.clear()
    sm.on_time(30.0)  # 确认 region:1 的 seek 完成
    sm.on_time(31.0)  # region:1 达 limit -> 循环回 region:0
    assert sm.snapshot()["current_song"]["id"] == "region:0"
    assert ("set_time", 0.0) in gw.calls
    assert ("play",) in gw.calls


def test_auto_next_off_limit_stop_no_next_no_mute(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_auto_next(False)
    sm.set_song_settings("region:0", {"end_action": "stop", "limit_s": 1.0, "mute_tracks": [3]})
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek 完成
    sm.on_time(1.0)
    assert sm.snapshot()["current_song"]["id"] == "region:0"
    assert sm.snapshot()["transport"]["playing"] is False
    assert find_call(gw, "set_track_mute", 3, True) == -1


def test_natural_end_auto_next_chains(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_auto_next(True)
    sm.play_song("region:0")
    assert sm.snapshot()["current_song"]["id"] == "region:0"
    sm.on_time(0.0)  # 确认 seek 完成
    sm.on_time(30.0)  # 自然播完 position >= end_s
    assert sm.snapshot()["current_song"]["id"] == "region:1"
    assert ("set_time", 30.0) in gw.calls


def test_natural_end_auto_next_off_highlights_by_position(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_auto_next(False)
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek 完成
    sm.on_time(30.0)
    assert sm.snapshot()["current_song"]["id"] == "region:1"
    assert ("stop",) not in gw.calls


def test_manual_stop_no_mute_keeps_current(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_song_settings("region:0", {"end_action": "stop_mute", "limit_s": 1.0, "mute_tracks": [3]})
    sm.play_song("region:0")
    gw.calls.clear()
    sm.stop()
    assert ("stop",) in gw.calls
    assert ("set_track_mute", 3, True) not in gw.calls
    snap = sm.snapshot()
    assert snap["current_song"]["id"] == "region:0"
    assert snap["transport"]["playing"] is False
    assert snap["clock"]["elapsed_s"] == 0.0  # 手动 stop 冻结 elapsed（position 未变）


def test_toggle_play_switches(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.toggle_play()
    assert sm.snapshot()["transport"]["playing"] is True
    assert ("play",) in gw.calls
    gw.calls.clear()
    sm.toggle_play()
    assert sm.snapshot()["transport"]["playing"] is False
    assert ("pause",) in gw.calls


def test_set_track_mute_manual_does_not_affect_restore_set(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_song_settings("region:0", {"end_action": "stop_mute", "limit_s": 1.0, "mute_tracks": [3]})
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek 完成
    sm.set_track_mute(1, True)  # REAPER 轨道 1
    assert ("set_track_mute", 1, True) in gw.calls
    gw.calls.clear()
    sm.on_time(1.0)  # 触发 stop_mute，恢复集合应只有 [3]，不含 1
    assert ("set_track_mute", 1, True) not in gw.calls
    assert ("set_track_mute", 3, True) in gw.calls
    gw.calls.clear()
    sm.play_song("region:1")  # 切歌只恢复 [3]
    assert ("set_track_mute", 3, False) in gw.calls
    assert ("set_track_mute", 1, False) not in gw.calls


def test_set_track_mute_one_based_track_number(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_track_mute(3, True)
    assert ("set_track_mute", 3, True) in gw.calls
    snap = sm.snapshot()
    assert snap["tracks"][2]["muted"] is True
    assert snap["tracks"][2]["index"] == 2


def test_play_song_restores_previous_mutes_before_jump(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_song_settings("region:0", {"end_action": "stop_mute", "limit_s": 1.0, "mute_tracks": [2]})
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek 完成
    sm.on_time(1.0)  # mute 2
    gw.calls.clear()
    sm.play_song("region:1")
    i_unmute = find_call(gw, "set_track_mute", 2, False)
    i_time = find_call(gw, "set_time", 30.0)
    assert i_unmute != -1 and i_time != -1 and i_unmute < i_time

def test_play_on_switch_defaults_true_and_persists(tmp_path):
    sm, store, _ = make_sm(tmp_path)
    sm.set_defaults(1.0, "stop", [1])
    assert sm.snapshot()["defaults"]["play_on_switch"] is True

    sm.set_defaults(1.0, "stop", [1], play_on_switch=False)
    assert sm.snapshot()["defaults"]["play_on_switch"] is False

    # 新状态机从同一 store 读取，play_on_switch 持久化
    sm2 = StateMachine(store, FakeOscGateway())
    assert sm2.snapshot()["defaults"]["play_on_switch"] is False


def test_play_song_autoplay_false_only_cues(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.play_song("region:0", autoplay=False)
    assert find_call(gw, "play") == -1
    assert find_call(gw, "stop") != -1
    assert find_call(gw, "set_time", 0.0) != -1
    snap = sm.snapshot()
    assert snap["transport"]["playing"] is False
    assert snap["current_song"]["id"] == "region:0"
    assert snap["clock"]["elapsed_s"] == 0.0

    # 之后按播放键：从 cue 点继续播放
    gw.calls.clear()
    sm.toggle_play()
    assert find_call(gw, "play") != -1
    assert sm.snapshot()["transport"]["playing"] is True


def test_seek_debounce_ignores_stale_position(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.set_auto_next(True)
    sm.play_song("region:0")
    # 旧 playhead 位置（上一曲 region:1 的 31s）先到达，不得触发 end/limit/auto_next
    sm.on_time(31.0)
    assert sm.snapshot()["current_song"]["id"] == "region:0"
    assert ("set_time", 30.0) not in gw.calls

    # 目标附近的位置到达后解除防抖，正常评估
    sm.on_time(0.0)
    sm.on_time(30.0)  # 自然播完 -> auto_next 下一首
    assert sm.snapshot()["current_song"]["id"] == "region:1"


def test_manual_stop_freezes_clock_ignores_edit_cursor_feedback(tmp_path):
    sm, _, gw = make_sm(tmp_path)
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek
    sm.on_time(1.5)
    assert sm.snapshot()["clock"]["elapsed_s"] == 1.5
    sm.stop()
    # REAPER 停止后经心跳回发 /time=编辑光标位置(0)，不得让时间码回零
    sm.on_time(0.0)
    snap = sm.snapshot()
    assert snap["transport"]["position_s"] == 1.5
    assert snap["clock"]["elapsed_s"] == 1.5


def test_snapshot_shape(tmp_path):
    sm, _, _ = make_sm(tmp_path)
    sm.on_track_name(1, "Click")
    sm.on_track_mute(2, True)
    sm.set_song_settings("region:0", {"limit_s": 5.0, "end_action": "none", "mute_tracks": [2]})
    sm.play_song("region:0")
    sm.on_time(0.0)  # 确认 seek 完成
    sm.on_time(2.0)
    snap = sm.snapshot()
    assert snap["type"] == "state"
    assert snap["transport"] == {"playing": True, "position_s": 2.0}
    assert snap["clock"]["elapsed_s"] == 2.0
    assert snap["clock"]["remaining_s"] == 3.0
    assert snap["clock"]["hhmmss"] == "0:02"
    assert snap["clock"]["hhmmss_remaining"] == "0:03"
    assert snap["current_song"]["id"] == "region:0"
    assert snap["current_song"]["limit_s"] == 5.0
    assert snap["current_song"]["end_action"] == "none"
    assert snap["current_song"]["mute_tracks"] == [2]
    assert snap["regions"][0]["id"] == "region:0"
    assert snap["tracks"][0] == {"index": 0, "name": "Click", "muted": False}
    assert snap["tracks"][1] == {"index": 1, "name": "", "muted": True}
    assert snap["defaults"]["end_action"] == "stop_mute"
    assert snap["song_settings"]["region:0"]["limit_s"] == 5.0


def test_hhmmss_format():
    assert hhmmss(0) == "0:00"
    assert hhmmss(65) == "1:05"
    assert hhmmss(725) == "12:05"
    assert hhmmss(-3) == "-0:03"


def test_hhmmss_countdown_format():
    assert hhmmss_countdown(3.0) == "0:03"
    assert hhmmss_countdown(2.95) == "0:03"  # 向上取整：不会一开播就少一秒
    assert hhmmss_countdown(2.0) == "0:02"
    assert hhmmss_countdown(0.0) == "0:00"
    assert hhmmss_countdown(65.1) == "1:06"
    assert hhmmss_countdown(-0.5) == "-0:00"


def test_countdown_display_not_one_second_short(tmp_path):
    sm, _, _ = make_sm(tmp_path)
    sm.set_song_settings("region:0", {"limit_s": 3.0, "end_action": "none", "mute_tracks": []})
    sm.play_song("region:0")
    sm.on_time(0.0)   # 确认 seek
    sm.on_time(0.05)  # 刚开播：remaining=2.95，仍应显示 0:03
    snap = sm.snapshot()
    assert snap["clock"]["remaining_s"] == pytest.approx(2.95)
    assert snap["clock"]["hhmmss_remaining"] == "0:03"
    sm.on_time(1.0)
    assert sm.snapshot()["clock"]["hhmmss_remaining"] == "0:02"


# ---------- REST ----------

@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", osc_reaper_port=9100, osc_listen_port=9101)
    store = Store(settings.data_dir)
    fake = FakeOscGateway()
    sm = StateMachine(store, fake)
    sm.add_region("region:0", "A", 0.0, 30.0)
    sm.add_region("region:1", "B", 30.0, 60.0)
    app = create_app(settings=settings, state_machine=sm, osc_gateway=fake)
    with TestClient(app) as c:
        yield c, sm, fake


def test_rest_health(client):
    c, _, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "version" in r.json()


def test_rest_state(client):
    c, _, _ = client
    r = c.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "state"
    assert "transport" in body and "clock" in body
    assert body["current_song"] is None  # 未播放过高亮不生效
    assert len(body["regions"]) == 2


def test_rest_regions_with_effective_settings(client):
    c, sm, _ = client
    sm.set_defaults(10.0, "stop", [1])
    sm.set_song_settings("region:0", {"limit_s": 5.0, "end_action": "none", "mute_tracks": [2]})
    r = c.get("/api/regions")
    assert r.status_code == 200
    regs = r.json()["regions"]
    assert regs[0]["id"] == "region:0"
    assert regs[0]["limit_s"] == 5.0
    assert regs[0]["end_action"] == "none"
    assert regs[0]["mute_tracks"] == [2]
    assert regs[1]["limit_s"] == 10.0
    assert regs[1]["end_action"] == "stop"
    assert regs[1]["mute_tracks"] == [1]


def test_rest_defaults_roundtrip(client):
    c, sm, _ = client
    r = c.put("/api/defaults", json={"limit_s": 8, "end_action": "stop", "mute_tracks": [1, 4], "play_on_switch": False})
    assert r.status_code == 200
    assert r.json()["defaults"]["limit_s"] == 8.0
    snap = c.get("/api/state").json()
    assert snap["defaults"] == {"limit_s": 8.0, "end_action": "stop", "mute_tracks": [1, 4], "play_on_switch": False}


def test_rest_song_settings_put_and_delete(client):
    c, _, _ = client
    r = c.put("/api/songs/region:0/settings", json={"limit_s": 3, "mute_tracks": [2]})
    assert r.status_code == 200
    assert r.json()["region:0"]["limit_s"] == 3.0
    assert "end_action" not in r.json()["region:0"]  # 部分更新：未提供字段不落库
    r = c.delete("/api/songs/region:0/settings")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert c.get("/api/state").json()["song_settings"] == {}


def test_rest_song_settings_unknown_region_404(client):
    c, _, _ = client
    assert c.put("/api/songs/region:9/settings", json={"limit_s": 1}).status_code == 404
    assert c.delete("/api/songs/region:9/settings").status_code == 404


def test_rest_auto_next(client):
    c, _, _ = client
    r = c.put("/api/auto_next", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["auto_next"] is True
    assert c.get("/api/state").json()["auto_next"] is True


def test_rest_transport_play_song_stop_toggle(client):
    c, _, _ = client
    r = c.post("/api/transport", json={"action": "play_song", "region_id": "region:1"})
    assert r.status_code == 200
    assert r.json()["current_song"]["id"] == "region:1"
    r = c.post("/api/transport", json={"action": "toggle_play"})
    assert r.status_code == 200
    assert r.json()["playing"] is False
    r = c.post("/api/transport", json={"action": "stop"})
    assert r.status_code == 200
    assert r.json()["stopped"] is True
    assert c.get("/api/state").json()["transport"]["playing"] is False


def test_rest_transport_errors(client):
    c, _, _ = client
    assert c.post("/api/transport", json={"action": "play_song"}).status_code == 422
    assert c.post("/api/transport", json={"action": "play_song", "region_id": "region:9"}).status_code == 404
    assert c.post("/api/transport", json={"action": "bogus"}).status_code == 422

def test_rest_transport_autoplay_false_passthrough(client):
    c, _, gw = client
    r = c.post("/api/transport", json={"action": "play_song", "region_id": "region:0", "autoplay": False})
    assert r.status_code == 200
    assert r.json()["current_song"]["id"] == "region:0"
    assert find_call(gw, "play") == -1
    assert find_call(gw, "stop") != -1

    gw.calls.clear()
    r = c.post("/api/transport", json={"action": "play_song", "region_id": "region:1"})
    assert r.status_code == 200
    assert find_call(gw, "play") != -1  # 默认 autoplay=True


# ---------- WS ----------

def test_ws_state_snapshot_and_ping(client):
    c, _, _ = client
    with c.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "state"
        assert "transport" in first
        ws.send_json({"cmd": "ping"})
        got = None
        for _ in range(50):
            m = ws.receive_json()
            if m.get("type") == "pong":
                got = m
                break
        assert got == {"type": "pong"}


def test_ws_state_broadcast_without_client_commands(client):
    c, _, _ = client
    import time

    with c.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "state"
        t0 = time.monotonic()
        second = ws.receive_json()  # 不发送任何客户端命令，等待广播 tick
        dt = time.monotonic() - t0
        assert second["type"] == "state"
        assert dt < 1.0  # 默认 10Hz 广播，第二个 state 应在约 1 秒内到达


def test_ws_command_ack(client):
    c, _, _ = client
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"cmd": "play_song", "region_id": "region:0"})
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["ok"] is True and ack["cmd"] == "play_song"

def test_ws_autoplay_and_play_on_switch_passthrough(client):
    c, sm, gw = client
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # 初始快照
        ws.send_json({"cmd": "play_song", "region_id": "region:0", "autoplay": False})
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["ok"] is True
        assert find_call(gw, "play") == -1
        assert find_call(gw, "stop") != -1

        ws.send_json({"cmd": "set_defaults", "settings": {
            "limit_s": 2.0, "end_action": "stop", "mute_tracks": [], "play_on_switch": False}})
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["ok"] is True
        assert sm.snapshot()["defaults"]["play_on_switch"] is False
        ws.send_json({"cmd": "play_song", "region_id": "region:9"})
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["ok"] is False and "未知 region" in ack["error"]
        ws.send_json({"cmd": "set_auto_next", "enabled": True})
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["ok"] is True
        ws.send_json({"cmd": "set_track_mute", "index": 1, "muted": True})
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["ok"] is True
        ws.send_json({"cmd": "bogus"})
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["ok"] is False


def test_ws_set_track_mute_rejects_invalid_index(client):
    c, sm, _ = client
    sm.on_track_name(1, "Click")

    def recv_until(pred):
        for _ in range(50):
            m = ws.receive_json()
            if pred(m):
                return m
        raise AssertionError("未收到预期 WS 消息")

    with c.websocket_connect("/ws") as ws:
        ws.receive_json()  # 初始快照
        before = recv_until(lambda m: m.get("type") == "state")["tracks"]

        for payload in (
            {"cmd": "set_track_mute", "index": 0, "muted": True},
            {"cmd": "set_track_mute", "muted": True},
        ):
            ws.send_json(payload)
            ack = recv_until(lambda m: m.get("type") == "ack")
            assert ack["ok"] is False
            assert "index>=1" in ack["error"]

        after = recv_until(lambda m: m.get("type") == "state")["tracks"]
        assert after == before


# ---------- python-osc 编解码 ----------

def test_osc_message_encoding_matches_contract():
    # trigger（无参）、float、int、string
    def encode(addr, *args):
        b = OscMessageBuilder(address=addr)
        for a in args:
            b.add_arg(a)
        return b.build()

    msg = encode("/play")
    assert msg.address == "/play"
    assert msg.params == []

    msg = encode("/time", 12.5)
    assert msg.address == "/time" and msg.params == [12.5]

    msg = encode("/track/3/mute", 1)
    assert msg.address == "/track/3/mute" and msg.params == [1]

    msg = encode("/region/1/name", "A")
    assert msg.address == "/region/1/name" and msg.params == ["A"]


class FakeCb:
    def __init__(self):
        self.events = []

    def on_time(self, v):
        self.events.append(("time", v))

    def on_play(self):
        self.events.append(("play",))

    def on_stop(self):
        self.events.append(("stop",))

    def on_pause(self):
        self.events.append(("pause",))

    def on_track_name(self, n, v):
        self.events.append(("track_name", n, v))

    def on_track_mute(self, n, v):
        self.events.append(("track_mute", n, v))

    def on_region_name(self, n, v):
        self.events.append(("region_name", n, v))

    def on_region_time(self, n, v):
        self.events.append(("region_time", n, v))

    def on_region_length(self, n, v):
        self.events.append(("region_length", n, v))


def test_osc_gateway_handlers_decode_feedback(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", osc_reaper_port=9100, osc_listen_port=9101)
    gw = OscGateway(settings)
    cb = FakeCb()
    gw.bind(cb)

    def invoke(handler, address, *args):
        handler(address, *args)

    invoke(gw._on_time, "/time", 12.5)
    invoke(gw._on_play, "/play", 1)
    invoke(gw._on_play, "/play", 0)
    invoke(gw._on_stop, "/stop")
    invoke(gw._on_pause, "/pause")
    invoke(gw._on_track_name, "/track/1/name", "Click")
    invoke(gw._on_track_mute, "/track/2/mute", 1)
    invoke(gw._on_region_name, "/region/1/name", "A")
    invoke(gw._on_region_time, "/region/1/time", 30.0)
    invoke(gw._on_region_length, "/region/1/length", 15.0)

    assert cb.events == [
        ("time", 12.5),
        ("play",),
        ("stop",),
        ("stop",),
        ("pause",),
        ("track_name", 1, "Click"),
        ("track_mute", 2, True),
        ("region_name", 1, "A"),
        ("region_time", 1, 30.0),
        ("region_length", 1, 15.0),
    ]


def test_osc_gateway_wildcard_int():
    assert OscGateway._wildcard_int("/track/3/mute", 2) == 3
    assert OscGateway._wildcard_int("/track/x/mute", 2) is None


def test_state_machine_region_upsert_from_osc(tmp_path):
    """OSC 反馈必须能创建 region（此前只更新已存在项，现场永远空列表）。"""
    settings = Settings(data_dir=tmp_path / "data")
    sm = StateMachine(Store(settings.data_dir), FakeOscGateway())
    sm.on_region_time(1, 10.0)
    sm.on_region_length(1, 20.0)
    sm.on_region_name(1, "开场曲")
    sm.on_region_name(2, "第二首")
    snap = sm.snapshot()
    assert [r["name"] for r in snap["regions"]] == ["开场曲", "第二首"]
    assert snap["regions"][0]["start_s"] == 10.0
    assert snap["regions"][0]["length_s"] == 20.0


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_osc_gateway_sends_from_bound_socket_and_heartbeats(tmp_path):
    """命令与心跳必须从监听端口（9101）发出，REAPER 仅接收模式才能回馈到位。"""
    reaper_port = _free_udp_port()
    listen_port = _free_udp_port()
    settings = Settings(
        data_dir=tmp_path / "data",
        osc_reaper_host="127.0.0.1",
        osc_reaper_port=reaper_port,
        osc_listen_host="127.0.0.1",
        osc_listen_port=listen_port,
    )
    gw = OscGateway(settings)
    gw.start()

    recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv.bind(("127.0.0.1", reaper_port))
    recv.settimeout(4.0)
    try:
        # 心跳（无需手动触发），源端口必须是监听端口
        data, addr = recv.recvfrom(4096)
        assert addr[1] == listen_port, f"心跳源端口应为 {listen_port}，实际 {addr[1]}"
        assert data.startswith(b"/device/track/follows/device"), data[:40]

        # 普通命令也从同一绑定 socket 发出（与握手余下消息交错，收集验证）
        gw._send("/play")
        recv.settimeout(0.5)
        deadline = time.time() + 4.0
        got_play = False
        while time.time() < deadline and not got_play:
            try:
                data, addr = recv.recvfrom(4096)
            except socket.timeout:
                continue
            assert addr[1] == listen_port
            if data.startswith(b"/play"):
                got_play = True
        assert got_play, "未收到从监听端口发出的 /play"
    finally:
        recv.close()
        gw.shutdown()


def test_osc_gateway_stop_is_transport_stop_not_teardown(tmp_path):
    """回归：gateway.stop() 只发 /stop 到 REAPER，绝不关闭自身监听 socket。"""
    reaper_port = _free_udp_port()
    listen_port = _free_udp_port()
    settings = Settings(
        data_dir=tmp_path / "data",
        osc_reaper_host="127.0.0.1",
        osc_reaper_port=reaper_port,
        osc_listen_host="127.0.0.1",
        osc_listen_port=listen_port,
    )
    gw = OscGateway(settings)
    gw.start()
    recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv.bind(("127.0.0.1", reaper_port))
    recv.settimeout(4.0)
    try:
        # 等一条心跳，确认已启动且源端口正确
        data, addr = recv.recvfrom(4096)
        assert addr[1] == listen_port
        assert gw.status()["listening"] is True

        gw.stop()  # transport stop
        deadline = time.time() + 4.0
        got_stop = False
        while time.time() < deadline and not got_stop:
            try:
                data, addr = recv.recvfrom(4096)
            except socket.timeout:
                continue
            assert addr[1] == listen_port
            if data.startswith(b"/stop"):
                got_stop = True
        assert got_stop, "未收到 /stop"
        assert gw.status()["listening"] is True
        assert gw.status()["last_error"] is None
    finally:
        recv.close()
        gw.shutdown()
    assert gw.status()["listening"] is False
