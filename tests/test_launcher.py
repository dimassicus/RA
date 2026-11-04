from scripts import serve_local


def test_local_env_loads_only_allowed_news_settings_without_overriding_shell(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# local keys\nRA_GNEWS_API_KEY='private-gnews'\n"
        "RA_THENEWSAPI_API_KEY=private-news\nRA_DATABASE_URL=do-not-load\n"
    )
    monkeypatch.setattr(serve_local, "ENV_FILE", env_file)
    environment = {"RA_GNEWS_API_KEY": "shell-key"}
    serve_local.load_local_env(environment)
    assert environment["RA_GNEWS_API_KEY"] == "shell-key"
    assert environment["RA_THENEWSAPI_API_KEY"] == "private-news"
    assert "RA_DATABASE_URL" not in environment


def test_launch_agent_supervises_foreground_without_embedding_secrets(monkeypatch):
    import plistlib
    monkeypatch.setenv("RA_GNEWS_API_KEY", "do-not-serialize")
    config = serve_local.service_definition()
    assert config["KeepAlive"] is True
    assert config["RunAtLoad"] is True
    assert config["ProgramArguments"][-1] == "run"
    assert config["ProgramArguments"][0].endswith("/.venv/bin/python")
    assert "do-not-serialize" not in plistlib.dumps(config).decode()


def test_foreground_waits_for_database_before_exec(tmp_path, monkeypatch):
    import psycopg
    import pytest
    calls = []
    monkeypatch.setattr(serve_local, "child_environment", lambda: {"RA_SECRET_KEY": "secret"})
    monkeypatch.setattr(serve_local.os, "chdir", lambda path: None)
    monkeypatch.setattr(serve_local, "PID_FILE", tmp_path / "pid")
    monkeypatch.setattr(serve_local.time, "sleep", lambda seconds: calls.append("wait"))
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql): calls.append(sql)
    def connect(*args, **kwargs):
        if not calls:
            calls.append("not ready")
            raise psycopg.OperationalError("database starting")
        return Connection()
    def execute(*args):
        calls.append("exec")
        raise SystemExit(0)
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(serve_local.os, "execve", execute)
    with pytest.raises(SystemExit):
        serve_local.run_foreground()
    assert calls == ["not ready", "wait", "SELECT 1", "exec"]
