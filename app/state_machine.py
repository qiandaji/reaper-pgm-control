"""纯逻辑状态机：可注入假 OSC 网关做单元测试（不 import python-osc）。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Optional

from .models import EndAction, SongSettings, hhmmss, hhmmss_countdown
from .store import Store

log = logging.getLogger("reaper-bridge.state")


def _secs(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f <= 0 else f


class StateMachine:
    """状态机。所有写操作在锁内完成；broadcast 循环（≈10Hz）调用 evaluate()。"""

    def __init__(self, store: Store, gateway: Any, transport_position_s: float = 0.0):
        self._store = store
        self._gateway = gateway
        self._lock = threading.RLock()
        self._transport = {"playing": False, "position_s": float(transport_position_s)}
        self._current: Optional[dict] = None
        self._started_at: Optional[float] = None  # wall-clock（time.time()）
        self._last_mute_set: set[int] = set()
        self._tracks: list[dict] = []
        self._ever_played = False
        self._regions: list[dict] = []  # 按 start_s 升序
        self._evaluate_guard = False
        self._end_fired = False  # 本曲 limit 到时动作已触发，避免每 0.1s 重复触发
        self._seek_pending = False  # set_time 后等待 REAPER 位置反馈确认，屏蔽过期位置触发误判
        self._seek_target: Optional[float] = None
        self._seek_started = 0.0

    # ---------- 供 OSC 网关回调 ----------
    def on_time(self, position: float) -> None:
        with self._lock:
            pos = float(position)
            confirmed = False
            if (
                self._seek_pending
                and self._seek_target is not None
                and abs(pos - self._seek_target) <= 0.5
            ):
                self._seek_pending = False
                confirmed = True
            # 手动 stop / cue 后冻结时间码：桥接未在播放且已选曲时，忽略
            # REAPER 经心跳回发的编辑光标位置（否则停止后时钟会跳回 0）。
            if not confirmed and not self._transport["playing"] and self._current is not None:
                return
            self._transport["position_s"] = pos
            self._evaluate_locked()

    def on_play(self) -> None:
        with self._lock:
            self._transport["playing"] = True
            if self._current is not None and self._started_at is None:
                self._started_at = datetime.now().timestamp()

    def on_stop(self) -> None:
        with self._lock:
            self._transport["playing"] = False

    def on_pause(self) -> None:
        with self._lock:
            self._transport["playing"] = False

    def on_track_name(self, reaper_number: int, name: str) -> None:
        with self._lock:
            idx = reaper_number - 1
            if idx < 0:
                return
            self._ensure_track_locked(idx)["name"] = str(name)

    def on_track_mute(self, reaper_number: int, muted: bool) -> None:
        with self._lock:
            idx = reaper_number - 1
            if idx < 0:
                return
            self._ensure_track_locked(idx)["muted"] = bool(muted)

    def on_region_name(self, reaper_number: int, name: str) -> None:
        with self._lock:
            if reaper_number < 1:
                return
            self._ensure_region_locked(reaper_number)["name"] = str(name)

    def on_region_time(self, reaper_number: int, start_s: float) -> None:
        with self._lock:
            if reaper_number < 1:
                return
            r = self._ensure_region_locked(reaper_number)
            r["start_s"] = float(start_s)
            r["end_s"] = r["start_s"] + r["length_s"]
            self._sort_regions_locked()

    def on_region_length(self, reaper_number: int, length_s: float) -> None:
        with self._lock:
            if reaper_number < 1:
                return
            r = self._ensure_region_locked(reaper_number)
            r["length_s"] = float(length_s)
            r["end_s"] = r["start_s"] + float(length_s)
            self._sort_regions_locked()

    def _find_region_by_reaper_locked(self, reaper_number: int) -> Optional[dict]:
        return next(
            (r for r in self._regions if r["reaper_number"] == reaper_number), None
        )

    def _ensure_region_locked(self, reaper_number: int) -> dict:
        r = self._find_region_by_reaper_locked(reaper_number)
        if r is None:
            idx = reaper_number - 1
            r = {
                "id": f"region:{idx}",
                "name": "",
                "start_s": 0.0,
                "end_s": 0.0,
                "length_s": 0.0,
                "index": idx,
                "reaper_number": reaper_number,
            }
            self._regions.append(r)
        return r

    # ---------- region 同步 ----------
    def set_regions(self, regions: list[dict]) -> None:
        with self._lock:
            self._regions = [dict(r) for r in regions]
            self._sort_regions_locked()
            if self._current is not None:
                for r in self._regions:
                    if r["id"] == self._current["id"]:
                        self._current.update({
                            "name": r["name"], "start_s": r["start_s"],
                            "end_s": r["end_s"], "length_s": r["length_s"],
                        })
                        break

    def add_region(self, region_id: str, name: str, start_s: float, end_s: float) -> None:
        with self._lock:
            self._regions.append({
                "id": region_id, "name": name, "start_s": float(start_s),
                "end_s": float(end_s), "length_s": float(end_s) - float(start_s),
                "index": int(region_id.split(":")[1]),
                "reaper_number": int(region_id.split(":")[1]) + 1,
            })
            self._sort_regions_locked()

    def _sort_regions_locked(self) -> None:
        self._regions.sort(key=lambda r: r["start_s"])

    def _ensure_track_locked(self, idx: int) -> dict:
        while len(self._tracks) <= idx:
            self._tracks.append({"index": len(self._tracks), "name": "", "muted": False})
        return self._tracks[idx]

    def _find_region_locked(self, region_id: str) -> Optional[dict]:
        return next((r for r in self._regions if r["id"] == region_id), None)

    # ---------- REST 操作 ----------
    def play_song(self, region_id: str, autoplay: bool = True) -> dict:
        with self._lock:
            region = self._find_region_locked(region_id)
            if region is None:
                raise KeyError(region_id)
            self._play_song_locked(region, autoplay=bool(autoplay))
            return dict(self._current)  # type: ignore[arg-type]

    def stop(self) -> None:
        with self._lock:
            self._gateway.stop()
            self._transport["playing"] = False

    def toggle_play(self) -> None:
        with self._lock:
            if self._transport["playing"]:
                self._gateway.pause()
                self._transport["playing"] = False
            elif self._end_fired and self._current is not None:
                # 本曲 limit 已到点停过：再按播放 = 从头重播本曲（恢复静音并重置计时）
                region = self._find_region_locked(self._current["id"]) or self._current
                self._play_song_locked(region)
            else:
                self._gateway.play()
                self._transport["playing"] = True
                self._end_fired = False
                if self._current is not None and self._started_at is None:
                    self._started_at = datetime.now().timestamp()

    def set_track_mute(self, track_number: int, muted: bool) -> None:
        """track_number 为 1-based REAPER 轨道号（与 CONTRACT §4/§5 一致）。"""
        with self._lock:
            self._gateway.set_track_mute(int(track_number), bool(muted))
            self._ensure_track_locked(int(track_number) - 1)["muted"] = bool(muted)

    def set_defaults(self, limit_s: Any, end_action: Any, mute_tracks: Any,
                     play_on_switch: Any = None) -> dict:
        with self._lock:
            from .models import Defaults

            d = Defaults(
                limit_s=_secs(limit_s),
                end_action=end_action or "stop_mute",
                mute_tracks=mute_tracks or [],
                play_on_switch=True if play_on_switch is None else bool(play_on_switch),
            )
            self._store.set_defaults(d)
            return d.model_dump(mode="json")

    def set_song_settings(self, region_id: str, fields: dict[str, Any]) -> dict:
        with self._lock:
            region = self._find_region_locked(region_id)
            if region is None:
                raise KeyError(region_id)
            ss = SongSettings.model_validate(fields)
            clean: dict[str, Any] = {}
            if "limit_s" in ss.model_fields_set:
                clean["limit_s"] = _secs(ss.limit_s)
            if "end_action" in ss.model_fields_set:
                clean["end_action"] = ss.end_action.value if ss.end_action is not None else None
            if "mute_tracks" in ss.model_fields_set:
                clean["mute_tracks"] = ss.mute_tracks or []
            saved = self._store.set_song_settings(region["name"], clean)
            return {region_id: saved}

    def clear_song_settings(self, region_id: str) -> bool:
        with self._lock:
            region = self._find_region_locked(region_id)
            if region is None:
                raise KeyError(region_id)
            return self._store.delete_song_settings(region["name"])

    def set_auto_next(self, enabled: bool) -> bool:
        with self._lock:
            return self._store.set_auto_next(enabled)

    def _effective_settings_locked(self, region: dict) -> dict:
        defaults = self._store.get_defaults().model_dump(mode="json")
        merged = dict(defaults)
        merged.update(self._store.get_song_settings(region["name"]))
        merged["end_action"] = merged.get("end_action") or "stop_mute"
        return merged

    # ---------- 内部播放逻辑 ----------
    def _restore_previous_mutes_locked(self) -> None:
        """切歌前：把上一首 muted 过的轨道 unmute（并发往 REAPER）。"""
        for track_number in sorted(self._last_mute_set):
            self._gateway.set_track_mute(track_number, False)
        self._last_mute_set = set()

    def _mute_region_tracks_locked(self, track_numbers: list[int]) -> None:
        for track_number in track_numbers:
            self._gateway.set_track_mute(int(track_number), True)
        self._last_mute_set = set(int(t) for t in track_numbers)

    def _play_song_locked(self, region: dict, autoplay: bool = True) -> None:
        self._restore_previous_mutes_locked()
        if autoplay:
            self._gateway.set_time(float(region["start_s"]))
            self._gateway.play()
            self._transport["playing"] = True
            self._started_at = datetime.now().timestamp()
        else:
            # 仅 cue：停止当前走带、跳到 region 起点，但不播放
            self._gateway.stop()
            self._gateway.set_time(float(region["start_s"]))
            self._transport["playing"] = False
            self._started_at = None
        self._current = {
            "id": region["id"],
            "name": region["name"],
            "index": region["index"],
            "start_s": region["start_s"],
            "end_s": region["end_s"],
            "length_s": region["length_s"],
        }
        self._ever_played = True
        self._end_fired = False
        self._seek_pending = True
        self._seek_target = float(region["start_s"])
        self._seek_started = time.time()

    def _stop_and_play_next_locked(self) -> None:
        self._gateway.stop()
        self._transport["playing"] = False
        if not self._regions:
            self._current = None
            return
        cur_index = self._current["index"] if self._current is not None else -1
        nxt = self._regions[(cur_index + 1) % len(self._regions)]
        self._play_song_locked(nxt)

    def _apply_end_action_locked(self, current: dict, settings: dict) -> None:
        action = settings.get("end_action") or "stop_mute"
        if self._store.get_auto_next():
            self._end_fired = True
            self._stop_and_play_next_locked()
            return
        if action == EndAction.none.value:
            return  # 不动作：不标记已触发，保持可继续播放/暂停
        self._end_fired = True
        self._gateway.stop()
        self._transport["playing"] = False
        if action == EndAction.stop_mute.value:
            self._mute_region_tracks_locked(settings.get("mute_tracks") or [])

    def _highlight_by_position_locked(self) -> None:
        """auto_next=false 且未达 limit 时：按位置自动高亮 current_song。"""
        if not self._ever_played:
            return
        pos = self._transport["position_s"]
        hit = next((r for r in self._regions if r["start_s"] <= pos < r["end_s"]), None)
        if hit is None:
            return
        cur = self._current
        if cur is None or cur["id"] != hit["id"]:
            self._current = {
                "id": hit["id"], "name": hit["name"], "index": hit["index"],
                "start_s": hit["start_s"], "end_s": hit["end_s"],
                "length_s": hit["length_s"],
            }
            self._started_at = None
            self._end_fired = False

    def _evaluate_locked(self) -> None:
        if self._evaluate_guard:
            return
        self._evaluate_guard = True
        try:
            if self._seek_pending:
                # set_time 后 REAPER 位置反馈可能夹杂旧值：
                # 在收到目标附近的新位置前（或 2s 兜底超时前）跳过到时/连播判断，
                # 避免切歌瞬间被旧 playhead 位置误触发「已到 end/limit」而随机切歌/停播。
                if time.time() - self._seek_started >= 2.0:
                    self._seek_pending = False
                else:
                    return
            if self._current is None:
                self._highlight_by_position_locked()
                return
            cur = self._current
            region = self._find_region_locked(cur["id"]) or cur
            settings = self._effective_settings_locked(region)
            limit = _secs(settings.get("limit_s"))
            if limit is not None and self._transport["position_s"] >= cur["start_s"] + limit:
                if not self._end_fired:
                    self._apply_end_action_locked(cur, settings)
                return
            if self._transport["position_s"] >= cur["end_s"]:
                if self._store.get_auto_next():
                    self._stop_and_play_next_locked()
                else:
                    self._highlight_by_position_locked()
        finally:
            self._evaluate_guard = False

    def evaluate(self) -> None:
        """供 broadcast 循环（≈10Hz）调用；无 current_song 时按位置高亮。"""
        with self._lock:
            self._evaluate_locked()

    # ---------- state 快照（REST /api/state 与 WS 广播同构） ----------
    def snapshot(self) -> dict:
        with self._lock:
            pos = self._transport["position_s"]
            current = self._current
            clock: dict = {
                "elapsed_s": 0.0,
                "remaining_s": None,
                "hhmmss": hhmmss(0.0),
                "hhmmss_remaining": None,
            }
            cur_out: Optional[dict] = None
            if current is not None:
                region = self._find_region_locked(current["id"]) or current
                settings = self._effective_settings_locked(region)
                limit = _secs(settings.get("limit_s"))
                elapsed = pos - current["start_s"]
                remaining = (limit - elapsed) if limit is not None else None
                clock = {
                    "elapsed_s": max(0.0, elapsed),
                    "remaining_s": remaining,
                    "hhmmss": hhmmss(max(0.0, elapsed)),
                    "hhmmss_remaining": hhmmss_countdown(remaining) if remaining is not None else None,
                }
                cur_out = {
                    "id": current["id"],
                    "name": current["name"],
                    "index": current["index"],
                    "start_s": current["start_s"],
                    "end_s": current["end_s"],
                    "length_s": current["length_s"],
                    "limit_s": limit,
                    "end_action": settings.get("end_action") or "stop_mute",
                    "mute_tracks": list(settings.get("mute_tracks") or []),
                }
            return {
                "type": "state",
                "transport": {
                    "playing": self._transport["playing"],
                    "position_s": self._transport["position_s"],
                },
                "clock": clock,
                "current_song": cur_out,
                "regions": [self._region_public(r) for r in self._regions if r["length_s"] > 0 or r["name"]],
                "tracks": [dict(t) for t in self._tracks],
                "auto_next": self._store.get_auto_next(),
                "defaults": self._store.get_defaults().model_dump(mode="json"),
                "song_settings": self._song_settings_public_locked(),
            }

    def regions_with_settings(self) -> list[dict]:
        with self._lock:
            out = []
            for r in self._regions:
                if r["length_s"] <= 0 and not r["name"]:
                    continue
                s = self._effective_settings_locked(r)
                out.append({
                    "id": r["id"], "name": r["name"], "index": r["index"],
                    "start_s": r["start_s"], "end_s": r["end_s"],
                    "length_s": r["length_s"],
                    "limit_s": s.get("limit_s"),
                    "end_action": s.get("end_action") or "stop_mute",
                    "mute_tracks": list(s.get("mute_tracks") or []),
                })
            return out

    @staticmethod
    def _region_public(r: dict) -> dict:
        return {
            "id": r["id"], "name": r["name"], "index": r["index"],
            "start_s": r["start_s"], "end_s": r["end_s"], "length_s": r["length_s"],
        }

    def _song_settings_public_locked(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self._regions:
            s = self._store.get_song_settings(r["name"])
            if s:
                out[r["id"]] = dict(s)
        return out