"""ADR-0011 AC-1: the dns_sources: + detection.dns_thrash: blocks parse to typed
config; malformed entries fail closed; absent blocks leave the feature off with zero
behavior change; a dns_thrash block with no dns_sources is a config error."""

from __future__ import annotations

import pytest

from wifi_shepard.config import (
    DnsInstanceSpec,
    DnsSourceSpec,
    DnsThrashConfig,
    build_config,
    load_config_from_path,
)


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


_VALID = """
dns_sources:
  - type: pihole
    password: pihole-pw
    instances:
      - url: http://192.168.1.186
      - url: https://192.168.1.189
detection:
  dns_thrash:
    same_domain_queries_max: 25
    window_minutes: 30
    sustain_windows: 3
"""


def test_both_blocks_parse_to_typed_config(tmp_path):
    config = load_config_from_path(_write(tmp_path, _VALID))

    assert isinstance(config.detection.dns_thrash, DnsThrashConfig)
    assert config.detection.dns_thrash.same_domain_queries_max == 25
    assert config.detection.dns_thrash.window_minutes == 30
    assert config.detection.dns_thrash.sustain_windows == 3

    assert len(config.dns_sources) == 1
    spec = config.dns_sources[0]
    assert isinstance(spec, DnsSourceSpec)
    assert spec.type == "pihole"
    assert spec.password == "pihole-pw"
    assert spec.instances == (
        DnsInstanceSpec(url="http://192.168.1.186"),
        DnsInstanceSpec(url="https://192.168.1.189"),
    )


def test_dns_thrash_defaults_when_keys_omitted(tmp_path):
    body = """
dns_sources:
  - type: pihole
    password: pw
    instances:
      - url: http://pi.hole
detection:
  dns_thrash: {}
"""
    config = load_config_from_path(_write(tmp_path, body))
    assert config.detection.dns_thrash == DnsThrashConfig(
        same_domain_queries_max=20, window_minutes=60, sustain_windows=2
    )


def test_absent_blocks_leave_feature_off_and_no_behavior_change(tmp_path):
    # A config with neither block must behave exactly as before: feature off.
    config = load_config_from_path(_write(tmp_path, "scanner:\n  dry_run: true\n"))
    assert config.detection.dns_thrash is None
    assert config.dns_sources == ()


def test_dns_thrash_without_dns_sources_is_a_config_error(tmp_path):
    body = """
detection:
  dns_thrash:
    same_domain_queries_max: 20
"""
    with pytest.raises(ValueError, match="dns_thrash is configured but no dns_sources"):
        load_config_from_path(_write(tmp_path, body))


def test_unknown_source_type_fails_closed(tmp_path):
    body = """
dns_sources:
  - type: adguard
    password: pw
    instances:
      - url: http://x
"""
    with pytest.raises(ValueError, match=r"dns_sources\[0\]\.type must be one of"):
        load_config_from_path(_write(tmp_path, body))


def test_missing_password_fails_closed(tmp_path):
    body = """
dns_sources:
  - type: pihole
    instances:
      - url: http://x
"""
    with pytest.raises(ValueError, match=r"dns_sources\[0\]\.password is required"):
        load_config_from_path(_write(tmp_path, body))


def test_instance_without_url_fails_closed(tmp_path):
    body = """
dns_sources:
  - type: pihole
    password: pw
    instances:
      - name: no-url-here
"""
    with pytest.raises(ValueError, match=r"dns_sources\[0\]\.instances\[0\]\.url is required"):
        load_config_from_path(_write(tmp_path, body))


def test_instance_url_without_scheme_fails_closed(tmp_path):
    body = """
dns_sources:
  - type: pihole
    password: pw
    instances:
      - url: 192.168.1.186
"""
    with pytest.raises(ValueError, match="must start with http:// or https://"):
        load_config_from_path(_write(tmp_path, body))


def test_empty_instances_fails_closed(tmp_path):
    body = """
dns_sources:
  - type: pihole
    password: pw
    instances: []
"""
    with pytest.raises(ValueError, match="must list at least one instance"):
        load_config_from_path(_write(tmp_path, body))


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("same_domain_queries_max", -1, "same_domain_queries_max"),
        ("same_domain_queries_max", "20", "same_domain_queries_max"),
        ("window_minutes", 0, "window_minutes must be >= 1"),
        ("sustain_windows", 0, "sustain_windows must be >= 1"),
        ("window_minutes", True, "window_minutes"),
    ],
)
def test_dns_thrash_fields_fail_closed(field, value, match):
    kwargs = {"same_domain_queries_max": 20, "window_minutes": 60, "sustain_windows": 2}
    kwargs[field] = value
    with pytest.raises(ValueError, match=match):
        build_config(
            dns_thrash=kwargs,
            dns_sources=[{"type": "pihole", "password": "pw", "instances": [{"url": "http://x"}]}],
        )
