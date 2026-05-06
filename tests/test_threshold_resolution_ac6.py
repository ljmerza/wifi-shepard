from __future__ import annotations


def test_ac_6_per_mac_override_beats_global_default():
    from wifi_shepard.config import build_config
    from wifi_shepard.scorer import resolve_thresholds

    overridden_mac = "dc:cc:e6:66:86:2b"
    other_mac = "11:22:33:44:55:66"

    config = build_config(
        tx_rate_kbps_max=12000,
        retry_pct_max=30,
        signal_dbm_max=-70,
        overrides=[{"mac": overridden_mac, "tx_rate_kbps_max": 6000}],
    )

    overridden = resolve_thresholds(overridden_mac, config)
    assert overridden["tx_rate_kbps_max"] == 6000, (
        "per-MAC override must win for the overridden field"
    )
    assert overridden["retry_pct_max"] == 30, (
        "non-overridden fields must fall back to the global default"
    )
    assert overridden["signal_dbm_max"] == -70

    other = resolve_thresholds(other_mac, config)
    assert other["tx_rate_kbps_max"] == 12000, (
        "non-overridden MAC must use global default"
    )
    assert other["retry_pct_max"] == 30
    assert other["signal_dbm_max"] == -70
