"""ADR-0011 AC-2: the Pi-hole password must never appear in a config-error message.

Same sink as the UNIFI_PASSWORD / HA_TOKEN cases (test_config_secret_redaction.py):
``${VAR}`` interpolation runs over the whole tree before structural validation, so a
rejected dns_sources fragment holds the live PIHOLE_PASSWORD. The message reaches
stderr at startup and container logs via config_reload_failed on SIGHUP, so it must
be redacted. ``password`` is already a _SECRET_KEYS entry — this pins that it stays
covered for the new block."""

from __future__ import annotations

import pytest

_PASSWORD = "Pihole-Admin-Pw-do-not-log"


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_dns_sources_as_mapping_does_not_leak_password(tmp_path, monkeypatch):
    """The one-character slip: `dns_sources:` written as a mapping because the leading
    `- ` was dropped. _require_sequence rejects it and repr's the whole (redacted) block."""
    from wifi_shepard.config import load_config_from_path

    monkeypatch.setenv("PIHOLE_PASSWORD", _PASSWORD)
    cfg = _write(
        tmp_path,
        """
dns_sources:
  type: pihole
  password: ${PIHOLE_PASSWORD}
  instances:
    - url: http://pi.hole
""",
    )

    with pytest.raises(ValueError) as exc:
        load_config_from_path(cfg)

    assert _PASSWORD not in str(exc.value), (
        "config shape error leaked the interpolated PIHOLE_PASSWORD into its message"
    )


def test_dns_sources_list_of_scalars_does_not_leak_password(tmp_path, monkeypatch):
    """A list whose entry is the bare interpolated secret (the sharp scalar edge)."""
    from wifi_shepard.config import load_config_from_path

    monkeypatch.setenv("PIHOLE_PASSWORD", _PASSWORD)
    cfg = _write(
        tmp_path,
        """
dns_sources:
  - ${PIHOLE_PASSWORD}
""",
    )

    with pytest.raises(ValueError) as exc:
        load_config_from_path(cfg)

    assert _PASSWORD not in str(exc.value)


def test_dns_source_spec_repr_does_not_contain_password():
    from wifi_shepard.config import DnsSourceSpec

    spec = DnsSourceSpec(type="pihole", password="super-secret-pihole-xyz", instances=())
    assert "super-secret-pihole-xyz" not in repr(spec)
