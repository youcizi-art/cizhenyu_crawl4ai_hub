"""Crawl4AI Hub 管理脚本（Windows）。

    python manage.py start      后台启动，等 /health 通过
    python manage.py status     查看健康状态
    python manage.py stop
    python manage.py logs -n 50
    python manage.py install    登录时自动启动（计划任务，无需管理员）
    python manage.py uninstall
    python manage.py serve      前台运行，看实时日志
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app.py"
STATE = Path(
    os.environ.get("C4AI_STATE_DIR")
    or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "crawl4ai-hub")
)
PID_FILE = STATE / "hub.pid"
LOG_FILE = STATE / "hub.log"
HUB_URL = os.environ.get("C4AI_HUB_URL", "http://127.0.0.1:11235").rstrip("/")
TASK_NAME = "Crawl4AI Hub"


def _python(windowless: bool = False) -> Path:
    exe = Path(sys.executable)
    if windowless:
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return exe


def _health(timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{HUB_URL}/health", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError, OSError):
        return None


def _pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in (out or "")


def cmd_serve(_: argparse.Namespace) -> int:
    return subprocess.run([str(_python()), str(APP)], cwd=str(ROOT)).returncode


def cmd_start(_: argparse.Namespace) -> int:
    if (health := _health()) is not None:
        print(f"already running: {HUB_URL} (uptime {health.get('uptime_s')}s)")
        return 0

    STATE.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("a", encoding="utf-8")
    log.write(f"\n=== start {datetime.now().isoformat(timespec='seconds')} ===\n")
    log.flush()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

    process = subprocess.Popen(
        [str(_python(windowless=True)), str(APP)],
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        creationflags=flags,
        close_fds=True,
    )
    PID_FILE.write_text(str(process.pid), encoding="utf-8")

    deadline = time.time() + 60
    while time.time() < deadline:
        if _health(timeout=2.0):
            print(f"started: {HUB_URL} (pid {process.pid})")
            print(f"log    : {LOG_FILE}")
            return 0
        if process.poll() is not None:
            print(f"error: hub exited during startup; see {LOG_FILE}", file=sys.stderr)
            return 1
        time.sleep(1.0)
    print(f"error: not healthy within 60s; see {LOG_FILE}", file=sys.stderr)
    return 1


def cmd_stop(_: argparse.Namespace) -> int:
    pid = _pid()
    if pid is None or not _alive(pid):
        print("not running")
        PID_FILE.unlink(missing_ok=True)
        return 0
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
    PID_FILE.unlink(missing_ok=True)
    print(f"stopped pid {pid}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    health = _health()
    if not health:
        print(f"down: {HUB_URL}")
        return 1
    print(json.dumps(health, indent=2, ensure_ascii=False))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    if not LOG_FILE.exists():
        print("no log yet")
        return 0
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.lines :]))
    return 0


def cmd_install(_: argparse.Namespace) -> int:
    command = f'"{_python(windowless=True)}" "{ROOT / "manage.py"}" start'
    result = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", command, "/F"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"schtasks failed:\n{result.stdout}\n{result.stderr}", file=sys.stderr)
        return 1
    print(f"installed logon task: {TASK_NAME}")
    return 0


def cmd_uninstall(_: argparse.Namespace) -> int:
    result = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"schtasks failed:\n{result.stdout}\n{result.stderr}", file=sys.stderr)
        return 1
    print(f"removed logon task: {TASK_NAME}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crawl4AI Hub 管理")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    sub.add_parser("start").set_defaults(func=cmd_start)
    sub.add_parser("stop").set_defaults(func=cmd_stop)
    sub.add_parser("status").set_defaults(func=cmd_status)
    logs = sub.add_parser("logs")
    logs.add_argument("-n", "--lines", type=int, default=40)
    logs.set_defaults(func=cmd_logs)
    sub.add_parser("install").set_defaults(func=cmd_install)
    sub.add_parser("uninstall").set_defaults(func=cmd_uninstall)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
