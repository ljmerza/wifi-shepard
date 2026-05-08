from __future__ import annotations

import pytest


def _write(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_radios_scalar_rejected_not_split_into_chars(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(tmp_path, "detection:\n  radios: ng\n")
    with pytest.raises(ValueError, match="detection.radios"):
        load_config_from_path(cfg)


def test_allowlist_scalar_rejected_not_split_into_chars(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(tmp_path, "allowlist: aa:bb:cc:dd:ee:ff\n")
    with pytest.raises(ValueError, match="allowlist"):
        load_config_from_path(cfg)


def test_overrides_must_be_a_list(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(tmp_path, "overrides: aa:bb:cc:dd:ee:ff\n")
    with pytest.raises(ValueError, match="overrides"):
        load_config_from_path(cfg)


def test_overrides_items_must_be_mappings(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(tmp_path, "overrides:\n  - aa:bb:cc:dd:ee:ff\n")
    with pytest.raises(ValueError, match=r"overrides\[0\]"):
        load_config_from_path(cfg)


def test_valid_sequences_still_load(tmp_path):
    from wifi_shepard.config import load_config_from_path

    cfg = _write(
        tmp_path,
        """
detection:
  radios: [ng, na]
allowlist:
  - aa:bb:cc:dd:ee:ff
overrides:
  - mac: 11:22:33:44:55:66
    tx_rate_kbps_max: 6000
""",
    )
    config = load_config_from_path(cfg)
    assert config.detection.radios == ("ng", "na")
    assert config.allowlist == ("aa:bb:cc:dd:ee:ff",)
    assert len(config.overrides) == 1
    assert config.overrides[0].mac == "11:22:33:44:55:66"
    assert config.overrides[0].tx_rate_kbps_max == 6000
