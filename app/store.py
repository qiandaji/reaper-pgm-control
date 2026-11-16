"""JSON 持久化：data/config.json（仿照 pgm-audio-server/app/store.py 风格）。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .models import Defaults


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._defaults = Defaults()
        self._songs: dict[str, dict[str, Any]] = {}
        self._auto_next = False
        self._load()

    @property
    def state_path(self) -> Path:
        return self.data_dir / "config.json"

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        self._defaults = Defaults.model_validate(raw.get("defaults", {}))
        songs = raw.get("songs", {})
        self._songs = {k: v for k, v in songs.items() if isinstance(v, dict)}
        self._auto_next = bool(raw.get("auto_next", False))

    def _save(self) -> None:
        payload = {
            "defaults": self._defaults.model_dump(mode="json"),
            "songs": self._songs,
            "auto_next": self._auto_next,
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        tmp.replace(self.state_path)

    def get_defaults(self) -> Defaults:
        with self._lock:
            return self._defaults.model_copy(deep=True)

    def set_defaults(self, defaults: Defaults) -> Defaults:
        with self._lock:
            self._defaults = defaults.model_copy(deep=True)
            self._save()
            return self._defaults.model_copy(deep=True)

    def get_song_settings(self, name: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._songs.get(name, {}))

    def set_song_settings(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merged = dict(self._songs.get(name, {}))
            merged.update(fields)
            self._songs[name] = merged
            self._save()
            return dict(merged)

    def delete_song_settings(self, name: str) -> bool:
        with self._lock:
            if name not in self._songs:
                return False
            del self._songs[name]
            self._save()
            return True

    def get_auto_next(self) -> bool:
        with self._lock:
            return self._auto_next

    def set_auto_next(self, enabled: bool) -> bool:
        with self._lock:
            self._auto_next = bool(enabled)
            self._save()
            return self._auto_next
