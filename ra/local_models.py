"""Run RA's local models one at a time within the Mac's memory budget."""
import fcntl
import json
import logging
from collections import deque
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import threading
import time

from scripts.translation_review.lmstudio import local_request as raw_request

PORT = 1234
QWEN = "qwen/qwen3.8-27b"
GEMMA = "google/gemma-4-31b-qat"
MODELS = {QWEN: "Qwen 27B", GEMMA: "Gemma 31B"}
CONTEXT = 16384
LOCK_PATH = Path(__file__).resolve().parents[1] / ".local" / "lmstudio.lock"
_lock = threading.Lock()
_activity = ""
_owner = threading.local()


def activity():
    return _activity


def cli_path():
    bundled = Path.home() / ".lmstudio" / "bin" / "lms"
    return str(bundled) if bundled.is_file() else shutil.which("lms")


def _command(*args):
    cli = cli_path()
    if not cli:
        raise OSError("LM Studio CLI missing. Install lms to enable automatic model switching.")
    try:
        result = subprocess.run([cli, *args], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("LM Studio model operation timed out; retry when the server is ready.") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise OSError("LM Studio could not " + args[0] + ": " + detail)
    return result.stdout


def _wait_idle():
    global _activity
    deadline = time.monotonic() + 600
    while True:
        loaded = json.loads(_command("ps", "--json"))
        if not any(row.get("status") != "idle" or row.get("queued", 0) for row in loaded
                   if row["identifier"] in MODELS):
            return loaded
        _activity = "Waiting for the current local-model task to finish."
        if time.monotonic() >= deadline:
            raise TimeoutError("Local model is still busy; retry after its current task finishes.")
        time.sleep(0.5)


def _ensure(model, port, *, retry=True):
    global _activity
    available = raw_request(port, "/api/v0/models", timeout=3)["data"]
    if not any(row["id"] == model for row in available):
        raise OSError(f"Download {MODELS[model]} in LM Studio before using it.")
    loaded = _wait_idle()
    ready = False
    for row in loaded:
        current = row["identifier"]
        if current not in MODELS:
            continue
        if (current == model and row.get("contextLength", 0) >= CONTEXT
                and row.get("parallel", 1) == 1):
            ready = True
            continue
        _activity = f"Unloading {MODELS[current]} to free memory…"
        _command("unload", current)
    if not ready:
        _activity = f"Loading {MODELS[model]}…"
        args = ["load", model, "--identifier", model, "--context-length", str(CONTEXT),
                "--parallel", "1", "--yes"]
        if model == GEMMA:
            args.extend(["--gpu", "max"])
        _command(*args)
        logging.getLogger(__name__).info("local_model_load batch=%s model=%s", _owner.label, model)
    deadline = time.monotonic() + 30
    models = raw_request(port, "/api/v0/models", timeout=3)["data"]
    while (any(row["id"] == model and row["state"] == "loading" for row in models)
           and time.monotonic() < deadline):
        time.sleep(0.5)
        models = raw_request(port, "/api/v0/models", timeout=3)["data"]
    active = [row for row in models if row["id"] in MODELS and row["state"] == "loaded"]
    if (len(active) != 1 or active[0]["id"] != model
            or active[0].get("loaded_context_length", 0) < CONTEXT):
        if retry:
            return _ensure(model, port, retry=False)
        raise OSError(f"{MODELS[model]} did not finish loading. Retry from Setup.")


@contextmanager
def batch(label):
    """Own the runtime across an entire set of dependent model passes, including CLI workers."""
    if getattr(_owner, 'label', None):
        yield
        return
    with _lock:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            _owner.label = label
            try:
                yield
            finally:
                global _activity
                _activity = ""
                _owner.label = None
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def run_phases(tasks, label, *, before_step=None):
    """Each task yields the model it needs next. Drain all ready work before switching."""
    waiting = {GEMMA: deque(), QWEN: deque()}
    results = []
    def advance(index, task):
        if before_step:
            before_step()
        try:
            model = next(task)
        except StopIteration as done:
            results[index] = done.value
        else:
            waiting[model].append((index, task))
    with batch(label):
        for task in tasks:
            index = len(results)
            results.append(None)
            advance(index, task)
        phase = 0
        while any(waiting.values()):
            for model in (GEMMA, QWEN):
                if waiting[model]:
                    phase += 1
                    logging.getLogger(__name__).info("model_phase batch=%s phase=%s model=%s tasks=%s",
                                                     label, phase, model, len(waiting[model]))
                while waiting[model]:
                    advance(*waiting[model].popleft())
    return results


def local_request(port, path, body=None, timeout=300):
    global _activity
    if path != "/v1/chat/completions":
        return raw_request(port, path, body, timeout)
    model = (body or {}).get("model")
    if port != PORT or model not in MODELS:
        raise ValueError("Unsupported RA local model or port")
    body = dict(body, reasoning_effort="none", chat_template_kwargs={
        **body.get("chat_template_kwargs", {}), "enable_thinking": False})
    with batch("individual request"):
        _ensure(model, port)
        _activity = f"{_owner.label} · {MODELS[model]} processing · reasoning off."
        return raw_request(port, path, body, timeout)
