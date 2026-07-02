"""TrailMem daemon configuration loader.

Reads ~/.trailmem/config.json. Creates default if missing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "watch_dir": "~/.claude/projects",
    "chunk_size": 10,
    "silence_minutes": 5,
    "llm_backend": "cli",  # "cli" | "api" | "openai"
    "llm_model": "sonnet",
    "anthropic_api_key": "",
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "db_path": "~/.trailmem/trailmem.db",
    "include_compaction_summary": False,
    "min_chunk_lines": 3,
    "forget_horizon_months": 6,
    "monthly_decay_factor": 0.5,
    "judge_max_items": 30,
    "flashback_buffer": "",
    "persona": "",
}


def config_dir() -> Path:
    d = Path(os.environ.get("TRAILMEM_HOME", "~/.trailmem")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def state_path() -> Path:
    return config_dir() / "state.json"


def pid_path() -> Path:
    return config_dir() / "daemon.pid"


def control_path() -> Path:
    return config_dir() / "control.json"


def log_path() -> Path:
    return config_dir() / "daemon.log"


def load_config(path: Path | None = None) -> dict[str, Any]:
    p = path or config_path()
    if not p.exists():
        save_config(DEFAULT_CONFIG, p)
        return dict(DEFAULT_CONFIG)
    with p.open() as f:
        loaded = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(loaded)
    # expand paths
    merged["watch_dir"] = str(Path(merged["watch_dir"]).expanduser())
    merged["db_path"] = str(Path(merged["db_path"]).expanduser())
    # env override for API keys
    if not merged.get("anthropic_api_key"):
        merged["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    if not merged.get("openai_api_key"):
        merged["openai_api_key"] = os.environ.get("OPENAI_API_KEY", "")
    return merged


def save_config(cfg: dict[str, Any], path: Path | None = None) -> None:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
