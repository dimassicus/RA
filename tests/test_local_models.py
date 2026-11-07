import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ra import local_models as lm


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    loaded = {lm.QWEN: lm.CONTEXT}
    calls = []
    available = set(lm.MODELS)
    monkeypatch.setattr(lm, "LOCK_PATH", tmp_path / "models.lock")
    monkeypatch.setattr(lm, "_activity", "")

    def command(*args):
        calls.append(args)
        if args[0] == "ps":
            return json.dumps([{"identifier": model, "contextLength": context, "status": "idle"}
                               for model, context in loaded.items()])
        if args[0] == "unload":
            del loaded[args[1]]
        if args[0] == "load":
            assert not loaded, "Never load the second large model before unloading the first"
            loaded[args[1]] = int(args[args.index("--context-length") + 1])
        return ""

    def request(port, path, body=None, timeout=300):
        if path == "/api/v0/models":
            return {"data": [{"id": model, "state": "loaded" if model in loaded else "not-loaded",
                              "loaded_context_length": loaded.get(model, 0)} for model in available]}
        assert list(loaded) == [body["model"]]
        assert body["reasoning_effort"] == "none"
        assert body["chat_template_kwargs"]["enable_thinking"] is False
        calls.append(("infer", body["model"]))
        return {"model": body["model"]}

    monkeypatch.setattr(lm, "_command", command)
    monkeypatch.setattr(lm, "raw_request", request)
    return loaded, calls, available


def infer(model):
    return lm.local_request(lm.PORT, "/v1/chat/completions", {"model": model})


def test_switches_both_ways_and_reuses_loaded_model(runtime):
    loaded, calls, _ = runtime
    infer(lm.GEMMA)
    infer(lm.GEMMA)
    infer(lm.QWEN)
    changes = [c for c in calls if c[0] != "ps"]
    assert [c[0] for c in changes] == ["unload", "load", "infer", "infer", "unload", "load", "infer"]
    assert changes[1][-2:] == ("--gpu", "max")
    assert changes[1][changes[1].index("--parallel") + 1] == "1"
    assert loaded == {lm.QWEN: lm.CONTEXT}
    assert not lm.activity()


def test_repairs_dual_loaded_state_and_insufficient_context(runtime):
    loaded, calls, _ = runtime
    loaded[lm.GEMMA] = lm.CONTEXT
    infer(lm.QWEN)
    assert ("unload", lm.GEMMA) in calls
    assert not any(c[0] == "load" for c in calls)
    loaded[lm.QWEN] = 8192
    infer(lm.QWEN)
    assert loaded == {lm.QWEN: lm.CONTEXT}


def test_missing_model_does_not_unload_working_model(runtime):
    loaded, calls, available = runtime
    available.remove(lm.GEMMA)
    with pytest.raises(OSError, match="Download Gemma"):
        infer(lm.GEMMA)
    assert loaded == {lm.QWEN: lm.CONTEXT}
    assert calls == []


def test_load_failure_never_sends_inference_and_allows_retry(runtime, monkeypatch):
    loaded, calls, _ = runtime
    command = lm._command

    def fail(*args):
        if args[0] == "load":
            raise OSError("insufficient memory")
        return command(*args)

    monkeypatch.setattr(lm, "_command", fail)
    with pytest.raises(OSError, match="insufficient memory"):
        infer(lm.GEMMA)
    assert not any(c[0] == "infer" for c in calls)
    assert not lm.activity()
    monkeypatch.setattr(lm, "_command", command)
    assert infer(lm.GEMMA)["model"] == lm.GEMMA


def test_concurrent_review_cannot_unload_active_translation(runtime, monkeypatch):
    _, calls, _ = runtime
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    raw = lm.raw_request

    def blocked(port, path, body=None, timeout=300):
        if body and body.get("model") == lm.GEMMA:
            entered.set()
            assert release.wait(5)
        return raw(port, path, body, timeout)

    def review():
        second_started.set()
        return infer(lm.QWEN)

    monkeypatch.setattr(lm, "raw_request", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        translation = pool.submit(infer, lm.GEMMA)
        assert entered.wait(5)
        review_task = pool.submit(review)
        try:
            assert second_started.wait(5)
            assert ("unload", lm.GEMMA) not in calls
        finally:
            release.set()
        assert translation.result()["model"] == lm.GEMMA
        assert review_task.result()["model"] == lm.QWEN
    assert calls.index(("infer", lm.GEMMA)) < calls.index(("unload", lm.GEMMA))


def test_busy_external_request_is_not_unloaded(runtime, monkeypatch):
    _, calls, _ = runtime
    monkeypatch.setattr(lm, "_command", lambda *a: json.dumps([
        {"identifier": lm.QWEN, "status": "generating", "queued": 0}]))
    ticks = iter([0, 601])
    monkeypatch.setattr(lm.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="still busy"):
        lm._wait_idle()
    assert calls == []


def test_get_requests_do_not_load_models(runtime):
    _, calls, _ = runtime
    assert lm.local_request(lm.PORT, "/api/v0/models")["data"]
    assert calls == []
