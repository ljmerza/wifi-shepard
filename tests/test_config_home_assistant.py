"""home_assistant: block parsing — PLAN.md §7, fail-closed like every other block."""

from __future__ import annotations

import pytest

_BASE = """
controllers: []
scanner:
  dry_run: true
"""

_HA_BLOCK = """
home_assistant:
  url: http://homeassistant:8123
  token: super-secret
  notify_service: mobile_app_pixel
"""


def _write(tmp_path, body: str):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body)
    return cfg


def test_home_assistant_block_parsed_into_config(tmp_path):
    from wifi_shepard.config import HomeAssistantConfig, load_config_from_path

    config = load_config_from_path(_write(tmp_path, _BASE + _HA_BLOCK))
    assert isinstance(config.home_assistant, HomeAssistantConfig)
    assert config.home_assistant.url == "http://homeassistant:8123"
    assert config.home_assistant.token == "super-secret"
    assert config.home_assistant.notify_service == "mobile_app_pixel"


def test_home_assistant_absent_means_notifications_off(tmp_path):
    from wifi_shepard.config import load_config_from_path

    config = load_config_from_path(_write(tmp_path, _BASE))
    assert config.home_assistant is None


@pytest.mark.parametrize("missing", ["url", "token", "notify_service"])
def test_home_assistant_missing_key_fails_closed(tmp_path, missing):
    from wifi_shepard.config import load_config_from_path

    block = {
        "url": "http://homeassistant:8123",
        "token": "super-secret",
        "notify_service": "mobile_app_pixel",
    }
    del block[missing]
    lines = "\n".join(f"  {k}: {v}" for k, v in block.items())
    cfg = _write(tmp_path, _BASE + "home_assistant:\n" + lines + "\n")
    with pytest.raises(ValueError, match=f"home_assistant.{missing}"):
        load_config_from_path(cfg)


def test_home_assistant_empty_token_from_env_fails_closed(tmp_path, monkeypatch):
    # The stated reason the block is fail-closed: an HA_TOKEN that interpolates
    # to "" must not silently ship a notifier that 401s on every kick.
    from wifi_shepard.config import load_config_from_path

    monkeypatch.setenv("HA_TOKEN", "")
    cfg = _write(
        tmp_path,
        _BASE
        + """
home_assistant:
  url: http://homeassistant:8123
  token: ${HA_TOKEN}
  notify_service: mobile_app_pixel
""",
    )
    with pytest.raises(ValueError, match="home_assistant.token"):
        load_config_from_path(cfg)


def test_home_assistant_non_mapping_fails_closed(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(tmp_path, _BASE + "home_assistant: yes\n")
    with pytest.raises(ValueError, match="home_assistant must be a YAML mapping"):
        load_config_from_path(cfg)


def test_home_assistant_url_requires_http_scheme(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(
        tmp_path,
        _BASE
        + """
home_assistant:
  url: homeassistant:8123
  token: super-secret
  notify_service: mobile_app_pixel
""",
    )
    with pytest.raises(ValueError, match="home_assistant.url"):
        load_config_from_path(cfg)


def test_home_assistant_config_repr_does_not_contain_token():
    from wifi_shepard.config import HomeAssistantConfig

    cfg = HomeAssistantConfig(
        url="http://homeassistant:8123",
        token="super-secret-token-xyz",
        notify_service="mobile_app_pixel",
    )
    assert "super-secret-token-xyz" not in repr(cfg)
