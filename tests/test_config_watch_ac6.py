"""ADR-0013 AC-6: the daemon hot-reloads on a config *file change* with no SIGHUP.

Mirrors test_sighup_ac7 but never signals the process — the file-watch task alone
must observe the edit (the SIGHUP path is unreliable in the container; see
Daemon._watch_config). A parse failure must keep the last-good config.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import FakeController
from tests.test_sighup_ac7 import _minimal_config_yaml


@pytest.mark.asyncio
async def test_ac_6_file_watch_reloads_valid_yaml_keeps_old_on_invalid(
    temp_db_path, tmp_path, fake_ha
):
    from wifi_shepard.main import build_daemon

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_minimal_config_yaml(poll_interval=10))

    fake = FakeController(clients=[])
    daemon = build_daemon(
        config_path=cfg_path,
        db_path=temp_db_path,
        controllers=[fake],
        ha=fake_ha,
        # Poll fast so the test doesn't wait on the 5s production default.
        config_watch_interval_seconds=0.05,
    )
    daemon_task = asyncio.create_task(daemon.run())

    try:
        await asyncio.wait_for(daemon.first_cycle_started.wait(), timeout=5)
        assert daemon.config.scanner.poll_interval_seconds == 10

        # Rewrite the file with NO signal — the watcher must pick it up on its own.
        cfg_path.write_text(_minimal_config_yaml(poll_interval=20))
        await asyncio.wait_for(daemon.config_reloaded.wait(), timeout=5)
        daemon.config_reloaded.clear()
        daemon.config_reload_attempted.clear()
        assert daemon.config.scanner.poll_interval_seconds == 20, (
            "file-watch must apply new config at the daemon level without SIGHUP"
        )
        assert daemon._scanners[0].poll_interval_seconds == 20, (
            "file-watch reload must propagate the new config to running scanners"
        )

        # A broken edit must be attempted-and-rejected, keeping the last-good config.
        cfg_path.write_text(": :: not valid :: yaml")
        await asyncio.wait_for(daemon.config_reload_attempted.wait(), timeout=5)
        assert not daemon.config_reloaded.is_set(), (
            "config_reloaded must NOT fire on a parse failure"
        )
        assert daemon.config.scanner.poll_interval_seconds == 20, (
            "an invalid file edit must keep the previous config"
        )
    finally:
        daemon.shutdown()
        await asyncio.wait_for(daemon_task, timeout=5)
