"""Start, check, or stop the RA development server outside the terminal session."""

from __future__ import annotations

import argparse
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".local"
PID_FILE = STATE / "ra_server.pid"
LOG_FILE = STATE / "ra_server.log"
SECRET_FILE = STATE / "secret_key"
PORT = int(os.environ.get("RA_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"


def healthy() -> bool:
    try:
        with urllib.request.urlopen(URL, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def is_ra_process(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
        check=False,
    )
    return result.returncode == 0 and "-m ra.app" in result.stdout


def start() -> int:
    if healthy():
        print(f"RA already responds at {URL}")
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(32))
        SECRET_FILE.chmod(0o600)
    environment = os.environ.copy()
    environment["RA_SECRET_KEY"] = SECRET_FILE.read_text().strip()
    with LOG_FILE.open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "ra.app"], cwd=ROOT, env=environment,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
    PID_FILE.write_text(str(process.pid))
    for _ in range(30):
        if healthy():
            print(f"RA started at {URL} (PID {process.pid})")
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.5)
    print(f"RA failed to start; see {LOG_FILE}", file=sys.stderr)
    return 1


def stop() -> int:
    pid = read_pid()
    if pid is None or not is_ra_process(pid):
        print("No RA server started by this script is running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not is_ra_process(pid):
            break
        time.sleep(0.25)
    PID_FILE.unlink(missing_ok=True)
    print("RA server stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"), nargs="?", default="start")
    action = parser.parse_args().action
    if action == "start":
        return start()
    if action == "stop":
        return stop()
    print(f"RA {'is running' if healthy() else 'is stopped'} at {URL}")
    return 0 if healthy() else 1


if __name__ == "__main__":
    raise SystemExit(main())
