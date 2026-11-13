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
from urllib.error import HTTPError

from scripts.translation_review.lmstudio import local_request as raw_request

PORT = 1234
QWEN = "qwen/qwen3.8-27b"
GEMMA = "google/gemma-4-31b-qat"
MODELS = {QWEN: "Qwen 27B", GEMMA: "Gemma 31B"}
CONTEXT = 16384
REVIEW_SLOTS = 4
LOCK_PATH = Path(__file__).resolve().parents[1] / ".local" / "lmstudio.lock"
_lock = threading.Lock()
_activity = (None, "")
_owner = threading.local()


def activity(batch_label=None):
    owner, message = _activity
    return message if batch_label is None or batch_label == owner else ""


def _set_activity(message):
    global _activity
    _activity = (getattr(_owner, "label", None), message)


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
    deadline = time.monotonic() + 600
    while True:
        loaded = json.loads(_command("ps", "--json"))
        if not any(row.get("status") != "idle" or row.get("queued", 0) for row in loaded
                   if row["identifier"] in MODELS):
            return loaded
        _set_activity("Waiting for the current local-model task to finish.")
        if time.monotonic() >= deadline:
            raise TimeoutError("Local model is still busy; retry after its current task finishes.")
        time.sleep(0.5)


def _ensure(model, port, *, retry=True):
    available = raw_request(port, "/api/v0/models", timeout=3)["data"]
    if not any(row["id"] == model for row in available):
        raise OSError(f"Download {MODELS[model]} in LM Studio before using it.")
    loaded = _wait_idle()
    slots = REVIEW_SLOTS if model == QWEN else 1
    ready = False
    for row in loaded:
        current = row["identifier"]
        if current not in MODELS:
            continue
        if (current == model and row.get("contextLength", 0) >= CONTEXT
                and row.get("parallel", 1) == slots):
            ready = True
            continue
        _set_activity(f"Unloading {MODELS[current]} to free memory…")
        _command("unload", current)
    if not ready:
        _set_activity(f"Loading {MODELS[model]}…")
        args = ["load", model, "--identifier", model, "--context-length", str(CONTEXT),
                "--parallel", str(slots), "--yes"]
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
            _owner.model = None
            try:
                yield
            finally:
                global _activity
                _activity = (None, "")
                _owner.label = None
                _owner.model = None
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


def _completion(port, path, body, timeout):
    body = dict(body, reasoning_effort="none", chat_template_kwargs={
        **body.get("chat_template_kwargs", {}), "enable_thinking": False})
    return raw_request(port, path, body, timeout)


def _ready_model(model, port):
    if _owner.model != model:
        _owner.model = None
        _ensure(model, port)
        _owner.model = model
    _set_activity(f"{_owner.label} · {MODELS[model]} processing · reasoning off.")


@contextmanager
def reviewer_requests():
    """The caller joins all concurrent reviews before releasing the model lease."""
    with batch("translation reviews"):
        _ready_model(QWEN, PORT)
        failed = threading.Event()

        def request(port, path, body, timeout):
            if port != PORT or path != "/v1/chat/completions" or body["model"] != QWEN:
                raise ValueError("Unexpected parallel review request")
            try:
                return _completion(port, path, body, timeout)
            except (OSError, ValueError):
                failed.set()
                raise

        try:
            yield request
        finally:
            if failed.is_set():
                _owner.model = None


def local_request(port, path, body=None, timeout=300):
    if path != "/v1/chat/completions":
        return raw_request(port, path, body, timeout)
    model = (body or {}).get("model")
    if port != PORT or model not in MODELS:
        raise ValueError("Unsupported RA local model or port")
    with batch("individual request"):
        cached = _owner.model == model
        _ready_model(model, port)
        try:
            return _completion(port, path, body, timeout)
        except HTTPError as exc:
            _owner.model = None
            if not cached or exc.code not in (400, 404, 409, 503):
                raise
            # An external unload can invalidate the lease. Retry only a missing instance.
            inventory = raw_request(port, "/api/v0/models", timeout=3)["data"]
            if any(row["id"] == model and row["state"] == "loaded" for row in inventory):
                raise
            _ensure(model, port)
            result = _completion(port, path, body, timeout)
            _owner.model = model
            return result
        except (OSError, ValueError):
            _owner.model = None
            raise
