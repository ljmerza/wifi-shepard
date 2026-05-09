from __future__ import annotations

import pytest


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_env_var_in_controller_password_is_interpolated(tmp_path, monkeypatch):
    from wifi_shepard.config import load_config_from_path

    monkeypatch.setenv("UNIFI_PASSWORD", "real-secret")
    cfg = _write(
        tmp_path,
        """
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: ${UNIFI_PASSWORD}
""",
    )
    config = load_config_from_path(cfg)
    assert config.controllers[0].password == "real-secret"


def test_missing_env_var_fails_closed_with_clear_message(tmp_path, monkeypatch):
    from wifi_shepard.config import load_config_from_path

    monkeypatch.delenv("UNIFI_PASSWORD", raising=False)
    cfg = _write(
        tmp_path,
        """
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: ${UNIFI_PASSWORD}
""",
    )
    with pytest.raises(ValueError, match=r"\$\{UNIFI_PASSWORD\}"):
        load_config_from_path(cfg)


def test_literal_strings_without_braces_pass_through(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(
        tmp_path,
        """
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: literal-not-interpolated
""",
    )
    config = load_config_from_path(cfg)
    assert config.controllers[0].password == "literal-not-interpolated"


def test_env_var_inside_string_substitutes_substring(tmp_path, monkeypatch):
    from wifi_shepard.config import load_config_from_path

    monkeypatch.setenv("UNIFI_HOST_SUFFIX", "1.1")
    cfg = _write(
        tmp_path,
        """
controllers:
  - type: unifi
    name: home
    host: 192.168.${UNIFI_HOST_SUFFIX}
    username: shepard
    password: secret
""",
    )
    config = load_config_from_path(cfg)
    assert config.controllers[0].host == "192.168.1.1"


def test_non_string_scalars_pass_through_untouched(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(
        tmp_path,
        """
scanner:
  poll_interval_seconds: 30
  window_samples: 3
  dry_run: false
""",
    )
    config = load_config_from_path(cfg)
    assert config.scanner.poll_interval_seconds == 30
    assert config.scanner.window_samples == 3
    assert config.scanner.dry_run is False
