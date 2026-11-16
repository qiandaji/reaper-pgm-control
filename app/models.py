"""配置（env 前缀 RPC_）与共享 pydantic 数据模型。

说明：为控制交付文件数量，原 app/config.py 的 Settings 并入本文件，
用法不变：from app.models import Settings, get_settings。
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VERSION = "0.1.0"


class Settings(BaseSettings):
    """应用配置，环境变量前缀 RPC_（如 RPC_PORT=61370）。"""

    model_config = SettingsConfigDict(env_prefix="RPC_", env_file=".env", extra="ignore")

    rpc_host: str = "127.0.0.1"
    rpc_port: int = 61370
    data_dir: Path = Path("data")
    # OSC：REAPER 控制面「监听端口」，桥接把命令发到这里
    osc_reaper_host: str = "127.0.0.1"
    osc_reaper_port: int = 9100
    # OSC：桥接自身监听端口，REAPER 把反馈发到这里
    osc_listen_host: str = "127.0.0.1"
    osc_listen_port: int = 9101
    # WS state 广播间隔（秒），约 10Hz
    broadcast_interval_s: float = 0.1

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()


def hhmmss(seconds: float) -> str:
    """M:SS 格式，分可超过 9（如 12:05），负值带 '-' 号。"""
    total = int(abs(seconds))
    minutes, secs = divmod(total, 60)
    sign = "-" if seconds < 0 else ""
    return f"{sign}{minutes}:{secs:02d}"


def hhmmss_countdown(seconds: float) -> str:
    """倒计时用 M:SS：正数向上取整，输入 0:03 从 0:03 开始倒数；负数同 hhmmss。"""
    if seconds < 0:
        return hhmmss(seconds)
    total = int(seconds)
    if float(total) != seconds:
        total += 1
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


class EndAction(str, Enum):
    none = "none"
    stop = "stop"
    stop_mute = "stop_mute"


def _validate_mute_tracks(v: Optional[list[int]]) -> Optional[list[int]]:
    if v is None:
        return v
    out = []
    for x in v:
        if isinstance(x, bool) or not isinstance(x, int) or x < 1:
            raise ValueError("mute_tracks 必须为 ≥1 的整数轨道号（与 REAPER 一致，1 起）")
        out.append(x)
    return out


class SongSettings(BaseModel):
    """歌曲设置；REST 部分更新用 model_fields_set 识别提供字段。"""

    model_config = ConfigDict(extra="ignore")

    limit_s: Optional[float] = None
    end_action: Optional[EndAction] = None
    mute_tracks: Optional[list[int]] = None

    @field_validator("limit_s")
    @classmethod
    def _check_limit(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError("limit_s 不能为负")
        return v

    @field_validator("mute_tracks")
    @classmethod
    def _check_mute(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        return _validate_mute_tracks(v)


class Defaults(BaseModel):
    limit_s: Optional[float] = None
    end_action: EndAction = EndAction.stop_mute
    mute_tracks: list[int] = Field(default_factory=list)
    play_on_switch: bool = True  # 手动切歌后是否自动播放

    @field_validator("limit_s")
    @classmethod
    def _check_limit(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError("limit_s 不能为负")
        return v

    @field_validator("mute_tracks")
    @classmethod
    def _check_mute(cls, v: list[int]) -> list[int]:
        return _validate_mute_tracks(v) or []


class RegionInfo(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str
    name: str
    index: int
    start_s: float
    end_s: float
    length_s: float
    reaper_number: int  # REAPER region 编号（1 起）


class TrackInfo(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    index: int  # WS 用 0 起；REAPER 轨道号 = index + 1
    name: str = ""
    muted: bool = False


class TransportState(BaseModel):
    playing: bool = False
    position_s: float = 0.0


class Clock(BaseModel):
    elapsed_s: float
    remaining_s: Optional[float] = None
    hhmmss: str
    hhmmss_remaining: Optional[str] = None


class CurrentSong(BaseModel):
    id: str
    name: str
    index: int
    start_s: float
    end_s: float
    length_s: float
    limit_s: Optional[float] = None
    end_action: str
    mute_tracks: list[int] = Field(default_factory=list)


class StateSnapshot(BaseModel):
    type: str = "state"
    transport: TransportState
    clock: Clock
    current_song: Optional[CurrentSong] = None
    regions: list[dict] = Field(default_factory=list)
    tracks: list[TrackInfo] = Field(default_factory=list)
    auto_next: bool = False
    defaults: dict = Field(default_factory=dict)
    song_settings: dict[str, dict] = Field(default_factory=dict)
