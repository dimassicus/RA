"""Start, check, or stop the RA development server outside the terminal session."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
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
ENV_FILE = ROOT / ".env"
PORT = int(os.environ.get("RA_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
LABEL = "com.ra.credit-monitor"
AGENT_FILE = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def service_definition() -> dict:
    """Keep credentials in .env, never in launchd arguments or the plist."""
    return {
        "Label": LABEL,
        "ProgramArguments": [str(ROOT / ".venv/bin/python"), str(Path(__file__).resolve()), "run"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 20,
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def launchctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *arguments], capture_output=True, text=True, check=False)


def service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def prepare_service() -> Path:
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f"{LABEL}.plist"
    path.write_bytes(plistlib.dumps(service_definition()))
    return path


def install_service() -> int:
    staged = prepare_service()
    if sys.platform != "darwin":
        print("Login startup requires macOS", file=sys.stderr)
        return 1
    # Stop only our identified process/job before handing ownership to launchd.
    if stop():
        return 1
    AGENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    AGENT_FILE.write_bytes(staged.read_bytes())
    AGENT_FILE.chmod(0o600)
    launchctl("enable", service_target())
    return start_service()


def start_service() -> int:
    if launchctl("print", service_target()).returncode != 0:
        result = launchctl("bootstrap", f"gui/{os.getuid()}", str(AGENT_FILE))
        if result.returncode:
            print("Could not load RA login service; check launchctl and the prepared plist", file=sys.stderr)
            return 1
    for _ in range(30):
        if healthy():
            print(f"RA login service is running at {URL}")
            return 0
        time.sleep(0.5)
    print(f"RA login service loaded; waiting for database/startup. Log: {LOG_FILE}")
    return 0


def child_environment() -> dict[str, str]:
    STATE.mkdir(parents=True, exist_ok=True)
    if not SECRET_FILE.exists():
        # Exclusive creation with private permissions from the first write.
        try:
            descriptor = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    environment = os.environ.copy()
    load_local_env(environment)
    environment["RA_SECRET_KEY"] = SECRET_FILE.read_text().strip()
    return environment


def run_foreground() -> int:
    """Wait through login's database race, then let launchd supervise the app PID."""
    environment = child_environment()
    os.chdir(ROOT)
    import psycopg

    waiting = False
    while True:
        try:
            with psycopg.connect(environment.get("RA_DATABASE_URL", "postgresql://localhost/RA"),
                                connect_timeout=3) as connection:
                connection.execute("SELECT 1")
            break
        except psycopg.OperationalError:
            if not waiting:
                print("RA is waiting for PostgreSQL", flush=True)
                waiting = True
            time.sleep(5)
    PID_FILE.write_text(str(os.getpid()))
    os.execve(sys.executable, [sys.executable, "-m", "ra.app"], environment)
    return 0


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


def load_local_env(environment: dict[str, str]) -> None:
    """Load local API settings into the child process without echoing secrets."""
    if not ENV_FILE.exists():
        return
    allowed = {"RA_GNEWS_API_KEY", "RA_THENEWSAPI_API_KEY",
               "RA_GNEWS_DAILY_LIMIT", "RA_THENEWSAPI_DAILY_LIMIT"}
    aliases = {"BRAVE API": "RA_BRAVE_API_KEY", "JINA API": "RA_JINA_API_KEY",
               "Firecrawler API": "RA_FIRECRAWL_API_KEY"}
    allowed.update(aliases.values())
    allowed.update({"RA_BRAVE_DAILY_LIMIT", "RA_JINA_DAILY_LIMIT", "RA_FIRECRAWL_DAILY_LIMIT"})
    for line in ENV_FILE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name, value = name.strip(), value.strip()
        name = aliases.get(name, name)
        if name not in allowed or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            environment.setdefault(name, value)


def start() -> int:
    if sys.platform == "darwin" and AGENT_FILE.exists():
        return start_service()
    if healthy():
        print(f"RA already responds at {URL}")
        return 0
    environment = child_environment()
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
    if sys.platform == "darwin" and launchctl("print", service_target()).returncode == 0:
        result = launchctl("bootout", service_target())
        if result.returncode:
            print("Could not stop the RA login service", file=sys.stderr)
            return 1
    pid = read_pid()
    if pid is None or not is_ra_process(pid):
        print("No RA server started by this script is running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(20):
        if not is_ra_process(pid):
            break
        time.sleep(0.25)
    PID_FILE.unlink(missing_ok=True)
    print("RA server stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop", "run", "prepare-service", "install-service"), nargs="?", default="start")
    action = parser.parse_args().action
    if action == "run":
        return run_foreground()
    if action == "prepare-service":
        print(prepare_service())
        return 0
    if action == "install-service":
        return install_service()
    if action == "start":
        return start()
    if action == "stop":
        return stop()
    print(f"RA {'is running' if healthy() else 'is stopped'} at {URL}")
    return 0 if healthy() else 1


if __name__ == "__main__":
    raise SystemExit(main())
