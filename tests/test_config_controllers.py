from __future__ import annotations

import pytest


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_controllers_block_parses_to_typed_tuple(tmp_path):
    from wifi_shepard.config import ControllerSpec, load_config_from_path

    cfg = _write(
        tmp_path,
        """
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: secret
    site: default
    verify_ssl: false
""",
    )
    config = load_config_from_path(cfg)
    assert len(config.controllers) == 1
    spec = config.controllers[0]
    assert isinstance(spec, ControllerSpec)
    assert spec.type == "unifi"
    assert spec.name == "home"
    assert spec.host == "192.168.1.1"
    assert spec.username == "shepard"
    assert spec.password == "secret"
    assert spec.site == "default"
    assert spec.verify_ssl is False


def test_controllers_block_absent_yields_empty_tuple(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(tmp_path, "scanner:\n  dry_run: true\n")
    config = load_config_from_path(cfg)
    assert config.controllers == ()


def test_controllers_empty_list_yields_empty_tuple(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(tmp_path, "controllers: []\n")
    config = load_config_from_path(cfg)
    assert config.controllers == ()


def test_controllers_missing_required_key_raises(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(
        tmp_path,
        """
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
""",
    )
    with pytest.raises(ValueError, match=r"controllers\[0\]\.password"):
        load_config_from_path(cfg)


def test_controllers_must_be_a_list(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(tmp_path, "controllers: not-a-list\n")
    with pytest.raises(ValueError, match="controllers"):
        load_config_from_path(cfg)


def test_controllers_default_site_and_verify_ssl(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(
        tmp_path,
        """
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: secret
""",
    )
    config = load_config_from_path(cfg)
    assert config.controllers[0].site == "default"
    assert config.controllers[0].verify_ssl is True, (
        "verify_ssl must default to True (secure-by-default) when omitted from YAML"
    )


def test_two_controllers_in_yaml_parse_to_two_specs(tmp_path):
    from wifi_shepard.config import ControllerSpec, load_config_from_path

    cfg = _write(
        tmp_path,
        """
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: secret
  - type: unifi
    name: garage
    host: 192.168.2.1
    username: shepard
    password: secret2
""",
    )
    config = load_config_from_path(cfg)
    assert len(config.controllers) == 2
    assert all(isinstance(c, ControllerSpec) for c in config.controllers)
    assert config.controllers[0].name == "home"
    assert config.controllers[0].host == "192.168.1.1"
    assert config.controllers[1].name == "garage"
    assert config.controllers[1].host == "192.168.2.1"
    assert config.controllers[0].password != config.controllers[1].password


@pytest.mark.xfail(
    strict=True,
    reason="ControllerSpec lacks port field — see review of PR #3 issue #1",
)
def test_controllers_port_from_yaml_is_preserved(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(
        tmp_path,
        """
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: secret
    port: 443
""",
    )
    config = load_config_from_path(cfg)
    assert config.controllers[0].port == 443


@pytest.mark.xfail(
    strict=True,
    reason="ControllerSpec uses default dataclass repr — see review of PR #3 issue #2",
)
def test_controller_spec_repr_does_not_contain_password():
    from wifi_shepard.config import ControllerSpec

    spec = ControllerSpec(
        type="unifi",
        name="home",
        host="192.168.1.1",
        username="shepard",
        password="super-secret-value-xyz",
    )
    assert "super-secret-value-xyz" not in repr(spec)
