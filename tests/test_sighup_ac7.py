from __future__ import annotations

import asyncio
import os
import signal

import pytest

from tests.conftest import FakeController, make_client


def _minimal_config_yaml(*, poll_interval: int = 10, allowlist: list[str] | None = None) -> str:
    allowlist_yaml = "[]" if not allowlist else "\n".join(f"  - {m}" for m in allowlist)
    if allowlist:
        allowlist_block = "allowlist:\n" + allowlist_yaml
    else:
        allowlist_block = "allowlist: []"
    return f"""
controllers: []
home_assistant:
  url: http://example.invalid:8123
  token: dummy-token
  notify_service: dummy
scanner:
  poll_interval_seconds: {poll_interval}
  window_samples: 1
  log_level: info
  log_format: human
  dry_run: true
detection:
  tx_rate_kbps_max: 12000
  retry_pct_max: 30
  signal_dbm_max: -70
  radios: [ng]
  ap_cu_total_min: 60
backoff:
  cooldowns_seconds: [300]
  max_kicks_per_hour: 3
  max_kicks_per_day: 10
  quarantine_after_kicks: 5
{allowlist_block}
overrides: []
"""


@pytest.mark.asyncio
async def test_ac_7_sighup_reloads_valid_yaml_keeps_old_on_invalid(temp_db_path, tmp_path, fake_ha):
    from wifi_shepard.main import build_daemon

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_minimal_config_yaml(poll_interval=10))

    bad_mac = "dc:cc:e6:66:86:2b"
    bad = make_client(
        mac=bad_mac, signal=-80, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100
    )
    fake = FakeController(clients=[bad])

    daemon = build_daemon(
        config_path=cfg_path, db_path=temp_db_path, controllers=[fake], ha=fake_ha
    )
    daemon_task = asyncio.create_task(daemon.run())

    try:
        await asyncio.wait_for(daemon.first_cycle_started.wait(), timeout=5)
        assert daemon.config.scanner.poll_interval_seconds == 10
        # Sanity: pre-reload there is no allowlist; the running scanner has [] too.
        assert daemon._scanners[0].config.allowlist == ()

        cfg_path.write_text(_minimal_config_yaml(poll_interval=20, allowlist=[bad_mac]))
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.wait_for(daemon.config_reloaded.wait(), timeout=5)
        daemon.config_reloaded.clear()
        daemon.config_reload_attempted.clear()
        assert daemon.config.scanner.poll_interval_seconds == 20, (
            "valid SIGHUP must apply new config at the daemon level"
        )
        assert daemon._scanners[0].config.allowlist == (bad_mac,), (
            "valid SIGHUP must propagate the new config to running scanners "
            "so the next scan cycle sees it (AC-7)"
        )
        assert daemon._scanners[0].poll_interval_seconds == 20

        cfg_path.write_text(": :: not valid :: yaml")
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.wait_for(daemon.config_reload_attempted.wait(), timeout=5)
        assert not daemon.config_reloaded.is_set(), "config_reloaded must NOT fire on parse failure"
        assert daemon.config.scanner.poll_interval_seconds == 20, (
            "invalid SIGHUP must keep previous config"
        )
        assert daemon._scanners[0].config.allowlist == (bad_mac,), (
            "invalid SIGHUP must keep the running scanner's previous config too"
        )
    finally:
        daemon.shutdown()
        await asyncio.wait_for(daemon_task, timeout=5)
