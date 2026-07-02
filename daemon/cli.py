"""trailmem-daemon CLI — start/stop/status/pause/resume."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from .config import (
        config_path,
        control_path,
        load_config,
        log_path,
        pid_path,
        state_path,
    )
    from .state import State
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from daemon.config import (  # type: ignore
        config_path,
        control_path,
        load_config,
        log_path,
        pid_path,
        state_path,
    )
    from daemon.state import State  # type: ignore


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _running_pid() -> int | None:
    pp = pid_path()
    if not pp.exists():
        return None
    try:
        pid = int(pp.read_text().strip())
    except ValueError:
        return None
    return pid if _pid_alive(pid) else None


def cmd_start(args: argparse.Namespace) -> int:
    pid = _running_pid()
    if pid:
        print(f"already running (pid={pid})")
        return 0
    cfg = load_config()
    if not args.yes:
        watch = cfg["watch_dir"]
        print(
            f"TrailMem daemon will watch {watch} and ingest conversation jsonls "
            f"into {cfg['db_path']}.\n"
            f"This passes conversation text to LLM backend '{cfg['llm_backend']}'.\n"
        )
        try:
            ans = input("Proceed? (y/N) ").strip().lower()
        except EOFError:
            ans = ""
        if ans != "y":
            print("aborted.")
            return 1
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "trailmem_daemon.daemon"]
    if args.verbose:
        cmd.append("--verbose")
    # ensure parent path so -m works
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
    # use direct module path
    cmd = [sys.executable, str(Path(__file__).with_name("daemon.py"))]
    if args.verbose:
        cmd.append("--verbose")
    proc = subprocess.Popen(
        cmd,
        stdout=open(log, "ab"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    # give the daemon a moment to write its pid
    for _ in range(20):
        time.sleep(0.1)
        if pid_path().exists():
            break
    new_pid = _running_pid()
    if new_pid:
        print(f"started (pid={new_pid}) — log: {log}")
        return 0
    print(f"failed to start; subprocess pid={proc.pid}; check log: {log}")
    return 2


def cmd_stop(_args: argparse.Namespace) -> int:
    pid = _running_pid()
    if not pid:
        print("not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.1)
        if not _pid_alive(pid):
            break
    if _pid_alive(pid):
        print(f"daemon (pid={pid}) did not stop in 5s")
        return 2
    print(f"stopped (was pid={pid})")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    pid = _running_pid()
    state = State()
    paused = False
    cp = control_path()
    if cp.exists():
        try:
            paused = bool(json.loads(cp.read_text()).get("paused", False))
        except (json.JSONDecodeError, OSError):
            pass
    print(f"daemon:   {'running pid=' + str(pid) if pid else 'stopped'}")
    print(f"paused:   {paused}")
    last = state.data.get("last_ingest_at")
    if last:
        ts = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
        print(f"last:     {ts}")
    else:
        print("last:     never")
    print(f"episodes: {state.data.get('episode_count', 0)}")
    files = state.data.get("files", {})
    print(f"tracked:  {len(files)} jsonl files")
    print(f"config:   {config_path()}")
    print(f"state:    {state_path()}")
    print(f"log:      {log_path()}")
    return 0


def _set_paused(value: bool) -> None:
    cp = control_path()
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps({"paused": value}))


def cmd_pause(_args: argparse.Namespace) -> int:
    _set_paused(True)
    print("paused (daemon will defer flushes)")
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    _set_paused(False)
    print("resumed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trailmem-daemon")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_start = sub.add_parser("start", help="start the daemon")
    p_start.add_argument("-y", "--yes", action="store_true",
                         help="skip confirmation prompt")
    p_start.add_argument("--verbose", action="store_true")
    p_start.set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the daemon").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="show status").set_defaults(func=cmd_status)
    sub.add_parser("pause", help="pause ingest").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="resume ingest").set_defaults(func=cmd_resume)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
