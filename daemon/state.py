"""Checkpoint state — tracks per-jsonl read offsets and last ingest time.

Persisted as JSON so the daemon can resume after restart.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import RLock
from typing import Any

from .config import state_path


class State:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_path()
        self._lock = RLock()
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"files": {}, "last_ingest_at": None, "episode_count": 0}
        try:
            with self.path.open() as f:
                d = json.load(f)
            d.setdefault("files", {})
            d.setdefault("last_ingest_at", None)
            d.setdefault("episode_count", 0)
            return d
        except (json.JSONDecodeError, OSError):
            return {"files": {}, "last_ingest_at": None, "episode_count": 0}

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w") as f:
                json.dump(self.data, f, indent=2)
            tmp.replace(self.path)

    # offset per jsonl file
    def get_offset(self, path: str) -> int:
        with self._lock:
            return int(self.data["files"].get(path, {}).get("offset", 0))

    def set_offset(self, path: str, offset: int) -> None:
        with self._lock:
            entry = self.data["files"].setdefault(path, {})
            entry["offset"] = int(offset)
            entry["updated_at"] = time.time()
            self.save()

    def get_last_seen(self, path: str) -> float:
        with self._lock:
            return float(self.data["files"].get(path, {}).get("last_seen", 0.0))

    def mark_seen(self, path: str) -> None:
        with self._lock:
            entry = self.data["files"].setdefault(path, {})
            entry["last_seen"] = time.time()
            self.save()

    # processed UUIDs guard — prevents double ingest if offsets get jittery
    def is_uuid_processed(self, uuid: str) -> bool:
        if not uuid:
            return False
        with self._lock:
            seen = self.data.setdefault("processed_uuids", [])
            return uuid in seen[-2000:]

    def mark_uuids(self, uuids: list[str]) -> None:
        if not uuids:
            return
        with self._lock:
            seen = self.data.setdefault("processed_uuids", [])
            seen.extend(u for u in uuids if u)
            # keep tail bounded
            if len(seen) > 5000:
                self.data["processed_uuids"] = seen[-5000:]
            self.save()

    def record_ingest(self, episode_count: int) -> None:
        with self._lock:
            self.data["last_ingest_at"] = time.time()
            self.data["episode_count"] = int(self.data.get("episode_count", 0)) + int(
                episode_count
            )
            self.save()
