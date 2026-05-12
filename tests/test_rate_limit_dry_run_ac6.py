"""ADR-0004 AC-6: dry_run bypasses rate limits.

With scanner.dry_run=true and min_seconds_between_kicks=999, two bad-state
clients in one cycle must both log would_kick (no kick_deferred), and no
wire-level call is made. The rate-limit state is not mutated either — dry-run
is observation, not action.
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
async def test_ac_6_dry_run_bypasses_rate_limits(temp_db_path, fake_ha, caplog):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    macs = ["aa:aa:aa:aa:aa:01", "bb:bb:bb:bb:bb:02"]
    fake = FakeController(clients=[_bad(macs[0], "ap1"), _bad(macs[1], "ap1")])
    config = build_config(
        dry_run=True,
        window_samples=1,
        safety_rails=dict(min_seconds_between_kicks=999, max_kicks_per_ap_per_window=1),
    )

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)

        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await scanner.run_once()

        # No wire calls at all.
        assert fake.force_reconnect_calls == []
        assert fake.btm_calls == []

        # Both clients logged would_kick.
        would_kick = [r for r in caplog.records if r.getMessage() == "would_kick"]
        macs_logged = sorted(getattr(r, "mac", None) for r in would_kick)
        assert macs_logged == sorted(macs), (
            f"AC-6: dry_run must log would_kick for every bad-state client; "
            f"got macs_logged={macs_logged}"
        )

        # No kick_deferred lines.
        deferred = [r for r in caplog.records if r.getMessage() == "kick_deferred"]
        assert deferred == [], (
            f"AC-6: dry_run must NOT emit kick_deferred (rate limits do not apply); got {deferred}"
        )

        # Rate-limit state untouched: after dry_run we should be able to flip to live
        # mode and immediately fire a kick (no recorded BTW/deauth on the timer).
        assert scanner.actor is not None
        rl = scanner.actor.rate_limiter
        assert rl is not None
        allowed, _, _ = rl.can_kick("ap1", now=0.0)
        assert allowed is True, (
            "AC-6: dry_run must not mutate rate-limit state — can_kick must return allowed=True"
        )
    finally:
        await db.close()
