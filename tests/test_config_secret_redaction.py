"""Config shape errors must never echo an interpolated secret.

``${VAR}`` interpolation runs over the whole YAML tree *before* structural validation,
so by the time a shape check fails the raw fragment holds the live UNIFI_PASSWORD /
HA_TOKEN. The frozen dataclasses carry ``field(repr=False)`` on those fields for exactly
this reason, but that guard cannot help here: what gets repr'd into the message is the
pre-dataclass plain dict.

Both sinks are real. At startup the traceback goes uncaught to stderr and into
``docker logs``; on SIGHUP, main.py's ``logger.exception("config_reload_failed")`` writes
the message into the container log. That log line is also precisely what an operator
pastes into a bug report when asking why startup failed. ADR-0001 states the opposite of
the old behavior: "UNIFI_PASSWORD and HA_TOKEN are never logged".
"""

from __future__ import annotations

import pytest

_PASSWORD = "S3cret-Admin-Pw-do-not-log"
_TOKEN = "ha-long-lived-token-do-not-log"


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_controllers_as_mapping_does_not_leak_password(tmp_path, monkeypatch):
    """The one-character YAML slip: `controllers:` written as a mapping because the
    leading `- ` was dropped. _require_sequence rejects it — and used to repr the whole
    block, live password included."""
    from wifi_shepard.config import load_config_from_path

    monkeypatch.setenv("UNIFI_PASSWORD", _PASSWORD)
    cfg = _write(
        tmp_path,
        """
controllers:
  type: unifi
  name: home
  host: 192.168.1.1
  username: shepard
  password: ${UNIFI_PASSWORD}
""",
    )

    with pytest.raises(ValueError) as exc:
        load_config_from_path(cfg)

    assert _PASSWORD not in str(exc.value), (
        "config shape error leaked the interpolated UNIFI_PASSWORD into its message; "
        "this message reaches docker logs at startup and via config_reload_failed on SIGHUP"
    )


def test_controllers_list_of_scalars_does_not_leak_password(tmp_path, monkeypatch):
    """`_require_mapping_items` path: a list whose entries aren't mappings."""
    from wifi_shepard.config import load_config_from_path

    monkeypatch.setenv("UNIFI_PASSWORD", _PASSWORD)
    cfg = _write(
        tmp_path,
        """
controllers:
  - ${UNIFI_PASSWORD}
""",
    )

    with pytest.raises(ValueError) as exc:
        load_config_from_path(cfg)

    assert _PASSWORD not in str(exc.value)


def test_home_assistant_as_list_does_not_leak_token(tmp_path, monkeypatch):
    from wifi_shepard.config import load_config_from_path

    monkeypatch.setenv("HA_TOKEN", _TOKEN)
    cfg = _write(
        tmp_path,
        """
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: pw

home_assistant:
  - base_url: http://ha.local:8123
    token: ${HA_TOKEN}
    service: mobile_app
""",
    )

    with pytest.raises(ValueError) as exc:
        load_config_from_path(cfg)

    assert _TOKEN not in str(exc.value), (
        "config shape error leaked the interpolated HA_TOKEN into its message"
    )


def test_nested_secret_is_redacted_not_just_top_level(tmp_path, monkeypatch):
    """Redaction must walk the tree — a secret nested inside a rejected list entry is
    just as leaked as one at the top level."""
    from wifi_shepard.config import load_config_from_path

    monkeypatch.setenv("UNIFI_PASSWORD", _PASSWORD)
    cfg = _write(
        tmp_path,
        """
controllers:
  home:
    - type: unifi
      password: ${UNIFI_PASSWORD}
""",
    )

    with pytest.raises(ValueError) as exc:
        load_config_from_path(cfg)

    assert _PASSWORD not in str(exc.value)


def test_shape_error_stays_debuggable(tmp_path, monkeypatch):
    """Redaction must not gut the message: the operator still needs to know which key
    was wrong, what type it got, and what the non-secret fields held."""
    from wifi_shepard.config import load_config_from_path

    monkeypatch.setenv("UNIFI_PASSWORD", _PASSWORD)
    cfg = _write(
        tmp_path,
        """
controllers:
  type: unifi
  name: home
  host: 192.168.1.1
  username: shepard
  password: ${UNIFI_PASSWORD}
""",
    )

    with pytest.raises(ValueError) as exc:
        load_config_from_path(cfg)
    msg = str(exc.value)

    assert "controllers" in msg, "error must still name the offending key"
    assert "dict" in msg, "error must still name the type it actually got"
    assert "192.168.1.1" in msg, "non-secret values must still be visible for debugging"
    assert "unifi" in msg


def test_redact_masks_secret_keys_and_leaves_others():
    from wifi_shepard.config import _redact

    raw = {
        "type": "unifi",
        "host": "192.168.1.1",
        "password": _PASSWORD,
        "nested": [{"token": _TOKEN, "service": "mobile_app"}],
    }

    out = _redact(raw)

    assert out["password"] != _PASSWORD
    assert out["nested"][0]["token"] != _TOKEN
    assert out["type"] == "unifi"
    assert out["host"] == "192.168.1.1"
    assert out["nested"][0]["service"] == "mobile_app"
    # The input must not be mutated — it is the live config on its way to build_config.
    assert raw["password"] == _PASSWORD


def test_redact_is_case_insensitive_on_key_names():
    from wifi_shepard.config import _redact

    out = _redact({"Password": _PASSWORD, "TOKEN": _TOKEN})

    assert _PASSWORD not in repr(out)
    assert _TOKEN not in repr(out)
