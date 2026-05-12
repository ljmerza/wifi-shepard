"""ADR-0004 AC-8: SIGHUP threshold change applies in place; in-flight state preserved.

Start with min_seconds_between_kicks=0 (off). Fire a kick at t=0. Reload config
with min_seconds_between_kicks=60. The next bad-state client within 60s of the
prior kick must be deferred — the in-memory _last_kick_at carries over.
"""

from __future__ import annotations

import logging

import pytest

from tests.conftest import FakeController, make_client


def _bad(mac: str, ap_id: str) -> object:
    return make_client(
        mac=mac,
        signal=-80,
        tx_rate_kbps=4000,
        tx_retries=60,
        wifi_tx_attempts=100,
        radio="ng",
        ap_id=ap_id,
    )


@pytest.mark.asyncio
async def test_ac_8_sighup_updates_thresholds_in_place_preserving_state(
    temp_db_path, fake_ha, caplog
):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    mac_a = "aa:aa:aa:aa:aa:01"
    mac_b = "bb:bb:bb:bb:bb:02"
    fake = FakeController(clients=[_bad(mac_a, "ap1")])
    config_off = build_config(dry_run=False, window_samples=1)  # safety_rails default off

    clock = [100.0]

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config_off, ha=fake_ha)
        assert scanner.actor is not None
        scanner.actor.now_fn = lambda: clock[0]

        # Cycle 1: a kicks; _last_kick_at = 100.0.
        await scanner.run_once()
        assert fake.force_reconnect_calls == [mac_a]

        # SIGHUP-style reload: turn on min_seconds_between_kicks=60.
        config_on = build_config(
            dry_run=False,
            window_samples=1,
            safety_rails=dict(min_seconds_between_kicks=60),
        )
        scanner.update_config(config_on)
        # update_config rewires the actor's config; verify the rate limiter picked up
        # the new threshold AND retained the prior _last_kick_at timestamp.
        assert scanner.actor is not None
        rl = scanner.actor.rate_limiter
        assert rl is not None
        assert rl.min_seconds_between_kicks == 60, (
            "AC-8: SIGHUP must propagate new threshold into the rate limiter"
        )
        assert rl._last_kick_at == 100.0, (
            "AC-8: in-flight rate-limit state (_last_kick_at) must NOT reset on reload"
        )

        # Cycle 2: B is bad-state, only 30s after the prior kick. New threshold must
        # gate it.
        clock[0] = 130.0
        fake.clients = [_bad(mac_b, "ap2")]
        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await scanner.run_once()

        # B was deferred — no additional wire call.
        assert fake.force_reconnect_calls == [mac_a], (
            f"AC-8: post-SIGHUP threshold must gate the next kick within window; "
            f"got {fake.force_reconnect_calls}"
        )
        deferred = [r for r in caplog.records if r.getMessage() == "kick_deferred"]
        assert any(getattr(r, "mac", None) == mac_b for r in deferred), (
            f"AC-8: B must produce a kick_deferred line under the new threshold; "
            f"got {[getattr(r, 'mac', None) for r in deferred]}"
        )
    finally:
        await db.close()
