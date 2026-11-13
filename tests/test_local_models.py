import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ra import local_models as lm


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    loaded = {lm.QWEN: lm.CONTEXT}
    slots = {lm.QWEN: lm.REVIEW_SLOTS}
    calls = []
    available = set(lm.MODELS)
    monkeypatch.setattr(lm, "LOCK_PATH", tmp_path / "models.lock")
    monkeypatch.setattr(lm, "_activity", (None, ""))

    def command(*args):
        calls.append(args)
        if args[0] == "ps":
            return json.dumps([{"identifier": model, "contextLength": context, "status": "idle",
                                "parallel": slots.get(model, 1)}
                               for model, context in loaded.items()])
        if args[0] == "unload":
            del loaded[args[1]]
        if args[0] == "load":
            assert not loaded, "Never load the second large model before unloading the first"
            slots[args[1]] = int(args[args.index("--parallel") + 1])
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


def test_model_listing_can_lag_cli_readiness(runtime, monkeypatch):
    _, calls, _ = runtime
    raw = lm.raw_request
    reads = 0
    def request(port, path, body=None, timeout=300):
        nonlocal reads
        response = raw(port, path, body, timeout)
        if path == '/api/v0/models':
            reads += 1
            if reads == 2:
                next(r for r in response['data'] if r['id'] == lm.QWEN)['state'] = 'loading'
        return response
    monkeypatch.setattr(lm, 'raw_request', request)
    monkeypatch.setattr(lm.time, 'sleep', lambda seconds: None)
    assert infer(lm.QWEN)['model'] == lm.QWEN
    assert reads == 3 and not any(c[0] == 'load' for c in calls)


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

    def translate():
        with lm.batch("run 51"):
            return infer(lm.GEMMA)

    def review():
        second_started.set()
        return infer(lm.QWEN)

    monkeypatch.setattr(lm, "raw_request", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        translation = pool.submit(translate)
        assert entered.wait(5)
        review_task = pool.submit(review)
        try:
            assert second_started.wait(5)
            assert "Gemma" in lm.activity("run 51")
            assert lm.activity("run 52") == ""
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


def test_whole_run_drains_each_model_before_corrections_or_competing_workers(runtime):
    _, calls, _ = runtime
    halfway, competing = threading.Event(), threading.Event()
    def work(i):
        yield lm.GEMMA
        infer(lm.GEMMA)
        if i == 0:
            halfway.set()
            assert competing.wait(5)
        yield lm.QWEN
        infer(lm.QWEN)
        if i % 4 == 0:
            yield lm.GEMMA
            infer(lm.GEMMA)
            yield lm.QWEN
            infer(lm.QWEN)
        return i
    def background():
        assert halfway.wait(5)
        competing.set()
        infer(lm.GEMMA)
    with ThreadPoolExecutor(max_workers=2) as pool:
        other = pool.submit(background)
        assert lm.run_phases((work(i) for i in range(24)), 'run 44') == list(range(24))
        other.result()
    assert [c[1] for c in calls if c[0]=='load'] == [lm.GEMMA,lm.QWEN,lm.GEMMA,lm.QWEN,lm.GEMMA]
    assert [c[1] for c in calls if c[0]=='infer'] == [lm.GEMMA]*24+[lm.QWEN]*24+[lm.GEMMA]*6+[lm.QWEN]*6+[lm.GEMMA]


def test_cached_readiness_expires_on_unlock_and_recovers_external_unload(runtime, monkeypatch):
    from urllib.error import HTTPError
    loaded, calls, _ = runtime
    raw = lm.raw_request

    def request(port, path, body=None, timeout=300):
        if body and body["model"] not in loaded:
            raise HTTPError(path, 404, "Model unloaded", {}, None)
        return raw(port, path, body, timeout)

    monkeypatch.setattr(lm, "raw_request", request)
    with lm.batch("test"):
        infer(lm.QWEN)
        infer(lm.QWEN)
        assert calls.count(("ps", "--json")) == 1
        loaded.clear()
        assert infer(lm.QWEN)["model"] == lm.QWEN
        assert calls.count(("ps", "--json")) == 2
    infer(lm.QWEN)
    assert calls.count(("ps", "--json")) == 3


def test_independent_reviews_use_one_lease_and_keep_evidence_in_order(runtime, monkeypatch):
    from ra import headline_review
    _, calls, _ = runtime
    raw = lm.raw_request
    ready = threading.Barrier(lm.REVIEW_SLOTS)

    def request(port, path, body=None, timeout=300):
        if body is None:
            return raw(port, path, body, timeout)
        source = json.loads(body["messages"][1]["content"])["source"]
        ready.wait(timeout=5)
        raw(port, path, body, timeout)
        return {"model": lm.QWEN, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "reviews": [{"candidate": "A", "verdict": "uncertain" if source.endswith("1") else "faithful",
                         "issues": []}]})}}]}

    monkeypatch.setattr(lm, "raw_request", request)
    items = [(f"Testquelle {i}", "de", f"Test source {i}", "") for i in range(lm.REVIEW_SLOTS)]
    results = headline_review.audit_batch(items)
    assert [r["status"] for r in results] == ["faithful", "uncertain", "faithful", "faithful"]
    assert calls.count(("ps", "--json")) == 1
    assert calls.count(("infer", lm.QWEN)) == 4
