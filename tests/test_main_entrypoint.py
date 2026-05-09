from __future__ import annotations

import logging
from pathlib import Path


class _StubDaemon:
    async def run(self) -> int:
        return 0


def test_run_forwards_env_paths_to_build_daemon(monkeypatch):
    from wifi_shepard import __main__ as entry

    captured: dict[str, Path] = {}

    def fake_build_daemon(*, config_path: Path, db_path: Path, **_: object) -> _StubDaemon:
        captured["config_path"] = config_path
        captured["db_path"] = db_path
        return _StubDaemon()

    exit_codes: list[int] = []
    monkeypatch.setattr(entry, "build_daemon", fake_build_daemon)
    monkeypatch.setattr(entry.sys, "exit", lambda code: exit_codes.append(code))
    monkeypatch.setattr(entry.logging, "basicConfig", lambda **_: None)
    monkeypatch.setenv("WIFI_SHEPARD_CONFIG", "/tmp/wifi-shepard-test-config.yaml")
    monkeypatch.setenv("WIFI_SHEPARD_DB", "/tmp/wifi-shepard-test-state.db")
    monkeypatch.delenv("WIFI_SHEPARD_LOG_LEVEL", raising=False)

    entry.run()

    assert captured["config_path"] == Path("/tmp/wifi-shepard-test-config.yaml")
    assert captured["db_path"] == Path("/tmp/wifi-shepard-test-state.db")
    assert exit_codes == [0]


def test_run_uses_documented_defaults_when_env_unset(monkeypatch):
    from wifi_shepard import __main__ as entry

    captured: dict[str, Path] = {}

    def fake_build_daemon(*, config_path: Path, db_path: Path, **_: object) -> _StubDaemon:
        captured["config_path"] = config_path
        captured["db_path"] = db_path
        return _StubDaemon()

    monkeypatch.setattr(entry, "build_daemon", fake_build_daemon)
    monkeypatch.setattr(entry.sys, "exit", lambda code: None)
    monkeypatch.setattr(entry.logging, "basicConfig", lambda **_: None)
    monkeypatch.delenv("WIFI_SHEPARD_CONFIG", raising=False)
    monkeypatch.delenv("WIFI_SHEPARD_DB", raising=False)

    entry.run()

    assert captured["config_path"] == Path("/config/config.yaml")
    assert captured["db_path"] == Path("/data/state.db")


def test_run_uppercases_log_level_env(monkeypatch):
    from wifi_shepard import __main__ as entry

    captured: dict[str, object] = {}

    def fake_basicconfig(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(entry.logging, "basicConfig", fake_basicconfig)
    monkeypatch.setattr(entry, "build_daemon", lambda **_: _StubDaemon())
    monkeypatch.setattr(entry.sys, "exit", lambda code: None)
    monkeypatch.setenv("WIFI_SHEPARD_LOG_LEVEL", "debug")

    entry.run()

    assert captured.get("level") == "DEBUG", (
        "lowercase env var must be uppercased before logging.basicConfig"
    )


def test_run_defaults_log_level_to_info_when_unset(monkeypatch):
    from wifi_shepard import __main__ as entry

    captured: dict[str, object] = {}
    monkeypatch.setattr(entry.logging, "basicConfig", lambda **kw: captured.update(kw))
    monkeypatch.setattr(entry, "build_daemon", lambda **_: _StubDaemon())
    monkeypatch.setattr(entry.sys, "exit", lambda code: None)
    monkeypatch.delenv("WIFI_SHEPARD_LOG_LEVEL", raising=False)

    entry.run()

    assert captured.get("level") == "INFO"
    # logging.basicConfig accepts the string "INFO" — sanity-check it resolves
    # to a real level so a typo would surface here, not at runtime.
    assert isinstance(logging.getLevelName("INFO"), int)
