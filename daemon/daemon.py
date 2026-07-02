"""TrailMem ingest daemon — watch ~/.claude/projects/**/*.jsonl and ingest.

Per-file buffer: accumulates new lines since last checkpoint. Flushes when
chunk_size reached OR silence_minutes elapsed since last write. Resumes from
saved offset across restarts.

Control: reads ~/.trailmem/control.json each tick for pause/resume.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Support both `python -m daemon.daemon` and `python daemon/daemon.py`.
try:
    from .config import (
        config_path,
        control_path,
        load_config,
        log_path,
        pid_path,
        state_path,
    )
    from .ingest import ensure_schema, ingest_chunk, maintenance
    from .state import State
except ImportError:  # script-mode invocation
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from daemon.config import (  # type: ignore
        config_path,
        control_path,
        load_config,
        log_path,
        pid_path,
        state_path,
    )
    from daemon.ingest import ensure_schema, ingest_chunk, maintenance  # type: ignore
    from daemon.state import State  # type: ignore

logger = logging.getLogger("trailmem.daemon")


# ---------------------------------------------------------------- buffer ----

def _count_conv_turns(lines: list[str]) -> int:
    """Count user/assistant conversation turns in raw JSONL lines."""
    count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        msg = entry.get("message", {})
        if msg.get("role") in ("user", "assistant"):
            content = msg.get("content", "")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if text.strip() and len(text.strip()) >= 10:
                count += 1
    return count


@dataclass
class FileBuffer:
    path: str
    pending: list[str] = field(default_factory=list)
    carryover: list[str] = field(default_factory=list)
    last_write: float = field(default_factory=time.time)
    line_offset: int = 0  # next line index to read

    def add(self, lines: list[str]) -> None:
        self.pending.extend(lines)
        self.last_write = time.time()

    def conv_turn_count(self) -> int:
        return _count_conv_turns(self.carryover + self.pending)


# ---------------------------------------------------------------- daemon ----

class TrailMemDaemon:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.state = State()
        self.buffers: dict[str, FileBuffer] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._observer: Observer | None = None
        # 起動から約1分後に最初のmaintenanceチェックが走るようにする
        # (実際にmaintenanceが仕事をするのは30日ゲートを通った時だけ)
        self._last_maintenance = time.time() - 3540.0

    # ------------- control file (pause/resume) -------------

    def _is_paused(self) -> bool:
        p = control_path()
        if not p.exists():
            return False
        try:
            with p.open() as f:
                return bool(json.load(f).get("paused", False))
        except (json.JSONDecodeError, OSError):
            return False

    # ------------- file read helpers -------------

    def _read_new_lines(self, fpath: str) -> list[str]:
        with self._lock:
            buf = self.buffers.setdefault(fpath, FileBuffer(path=fpath))
            if buf.line_offset == 0:
                # restore from state
                buf.line_offset = self.state.get_offset(fpath)
        try:
            with open(fpath, encoding="utf-8") as f:
                all_lines = f.readlines()
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("read failed %s: %s", fpath, e)
            return []
        new = all_lines[buf.line_offset :]
        if new:
            with self._lock:
                buf.add(new)
                buf.line_offset = len(all_lines)
        return new

    # ------------- main loop -------------

    def _bootstrap_existing(self, watch_dir: Path) -> None:
        """On startup, scan jsonl files but do not re-ingest history.
        Set offsets to current EOF for any unseen files, so we only catch
        future writes (avoids LLM-flooding on first launch)."""
        for jsonl in watch_dir.rglob("*.jsonl"):
            spath = str(jsonl)
            if spath in self.state.data.get("files", {}):
                continue  # already tracked
            try:
                with jsonl.open() as f:
                    n_lines = sum(1 for _ in f)
            except OSError:
                continue
            self.state.set_offset(spath, n_lines)
            logger.info("bootstrap: skip-to-end %s (%d lines)", jsonl.name, n_lines)

    def _flush_if_due(self, fpath: str, *, force: bool = False) -> None:
        with self._lock:
            buf = self.buffers.get(fpath)
            if not buf or (not buf.pending and not buf.carryover):
                return
            # carryoverだけで新しい行がない場合はflushしない(無限ループ防止)
            if buf.carryover and not buf.pending:
                return
            now = time.time()
            silence = float(self.cfg.get("silence_minutes", 5)) * 60.0
            chunk_size = int(self.cfg.get("chunk_size", 50))
            turns = buf.conv_turn_count()
            size_ok = turns >= chunk_size
            silence_ok = (now - buf.last_write) >= silence and turns > 0
            if not (force or size_ok or silence_ok):
                return
            # take ownership of carryover + pending lines
            lines = buf.carryover + buf.pending
            buf.carryover = []
            buf.pending = []

        if self._is_paused():
            with self._lock:
                self.buffers[fpath].pending = lines + self.buffers[fpath].pending
            logger.info("paused — deferring flush for %s", Path(fpath).name)
            return

        conv_turns = _count_conv_turns(lines)
        logger.info("flushing %d lines (%d conv turns) from %s",
                    len(lines), conv_turns, Path(fpath).name)
        try:
            n, uuids = ingest_chunk(self.cfg, jsonl_path=fpath, new_lines=lines)
        except Exception as e:  # noqa: BLE001
            logger.exception("ingest failed for %s: %s", fpath, e)
            return
        if n > 0:
            self.state.mark_uuids(uuids)
            self.state.record_ingest(n)
            logger.info("ingested %d episodes from %s", n, Path(fpath).name)
        elif n == 0 and conv_turns > 0:
            # LLM returned empty array — topic still in progress, carry over
            with self._lock:
                self.buffers[fpath].carryover = lines
            logger.info("no episodes (carryover %d lines) from %s",
                       len(lines), Path(fpath).name)
            return
        else:
            logger.info("no episodes extracted from %s", Path(fpath).name)
        # persist offset only after successful processing
        with self._lock:
            buf = self.buffers[fpath]
            self.state.set_offset(fpath, buf.line_offset)
            self.state.mark_seen(fpath)

    def _watchdog_handler(self) -> FileSystemEventHandler:
        outer = self

        class _Handler(FileSystemEventHandler):
            @staticmethod
            def _should_skip(path: str) -> bool:
                basename = os.path.basename(path)
                return basename.startswith("agent-")

            def on_modified(self, event):
                if event.is_directory:
                    return
                if not event.src_path.endswith(".jsonl"):
                    return
                if self._should_skip(event.src_path):
                    return
                outer._read_new_lines(event.src_path)

            def on_created(self, event):
                if event.is_directory:
                    return
                if not event.src_path.endswith(".jsonl"):
                    return
                if self._should_skip(event.src_path):
                    return
                outer._read_new_lines(event.src_path)

            def on_moved(self, event):
                if event.is_directory:
                    return
                dest = getattr(event, "dest_path", "")
                if dest.endswith(".jsonl") and not self._should_skip(dest):
                    outer._read_new_lines(dest)

        return _Handler()

    def run(self) -> None:
        watch_dir = Path(self.cfg["watch_dir"]).expanduser()
        if not watch_dir.exists():
            logger.error("watch_dir does not exist: %s", watch_dir)
            sys.exit(2)
        ensure_schema(self.cfg["db_path"])
        logger.info("daemon starting — watch=%s db=%s backend=%s",
                    watch_dir, self.cfg["db_path"], self.cfg.get("llm_backend"))

        self._bootstrap_existing(watch_dir)

        self._observer = Observer()
        self._observer.schedule(self._watchdog_handler(), str(watch_dir), recursive=True)
        self._observer.start()

        # write pid
        pid_path().write_text(str(os.getpid()))

        try:
            while not self._stop.is_set():
                # tick every 5s for silence-flush checks
                self._stop.wait(5.0)
                with self._lock:
                    paths = list(self.buffers.keys())
                for p in paths:
                    self._flush_if_due(p)
                # cron不要化: 1時間おきにmaintenanceを叩く。実際に仕事をするのは
                # 30日ゲートを通った時だけ (ingest.maintenance側で判定)。
                now = time.time()
                if now - self._last_maintenance >= 3600.0:
                    self._last_maintenance = now
                    try:
                        stats = maintenance(self.cfg)
                        if stats:
                            logger.info("maintenance: %s", stats)
                    except Exception:  # noqa: BLE001
                        logger.exception("maintenance failed")
        finally:
            logger.info("stopping...")
            self._observer.stop()
            self._observer.join(timeout=5)
            # final flush on stop
            with self._lock:
                paths = list(self.buffers.keys())
            for p in paths:
                self._flush_if_due(p, force=True)
            try:
                pid_path().unlink()
            except FileNotFoundError:
                pass

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------- bootstrap


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    log_file = log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file),
    ]
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    # watchdog is very chatty at DEBUG — keep it at INFO regardless of --verbose
    logging.getLogger("watchdog").setLevel(logging.INFO)


def check_hook_conflict() -> str | None:
    """Detect competing UserPromptSubmit hook (CC hook-mode trailmem). Return warning text or None."""
    candidates = [
        Path("~/.claude/settings.json").expanduser(),
        Path("~/.claude/settings.local.json").expanduser(),
    ]
    hits: list[str] = []
    for c in candidates:
        if not c.exists():
            continue
        try:
            data = json.loads(c.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        hooks = data.get("hooks", {}).get("UserPromptSubmit", [])
        for group in hooks:
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                if "trailmem_hook" in cmd or "trailmem-auto-ingest" in cmd:
                    hits.append(f"{c}: {cmd}")
    if hits:
        return (
            "WARNING: existing CC hook-based trailmem ingest found:\n  "
            + "\n  ".join(hits)
            + "\nRunning the daemon alongside the hook will produce duplicate episodes."
            "\nDisable the hook in settings.json before relying on the daemon."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TrailMem ingest daemon")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--foreground", action="store_true",
                        help="run in foreground (default behavior)")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    cfg_path = Path(args.config) if args.config else config_path()
    cfg = load_config(cfg_path)

    warn = check_hook_conflict()
    if warn:
        logger.warning(warn)

    daemon = TrailMemDaemon(cfg)

    def _sig(_signum, _frame):
        logger.info("signal received, shutting down")
        daemon.stop()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
