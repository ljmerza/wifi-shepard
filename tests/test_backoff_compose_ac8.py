"""ADR-0007 AC-8: the per-MAC backoff composes with — does not replace — the
existing gates.

- Quarantine (ADR-0001 AC-5) still gates first, even with caps/cooldowns on.
- The BTM->deauth fallback (ADR-0003) is the same logical kick and must NOT be
  re-gated by the per-MAC cooldown.
"""

from __future__ import annotations

import pytest

from tests.conftest import FakeController, make_client


def _bad(mac: str, ap_id: str = "ap1"):
    return make_client(
        mac=mac, signal=-80, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100, ap_id=ap_id
    )


@pytest.mark.asyncio
async def test_ac8_quarantine_still_gates_with_caps_on(temp_db_path, fake_ha):
    from wifi_shepard.backoff import State
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    config = build_config(
        dry_run=False,
        window_samples=1,
        quarantine_after_kicks=5,
        max_kicks_per_day=10,
        cooldowns_seconds=[300, 1800],
    )

    db = Database(temp_db_path)
    await db.connect()
    try:
        fake = FakeController(clients=[_bad(bad_mac)])
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        for _ in range(5):
            scanner.backoff.record_kick(bad_mac)
        fake.force_reconnect_calls.clear()
        fake_ha.posts.clear()

        await scanner.run_once()

        assert fake.force_reconnect_calls == [], (
            "AC-8: quarantine must gate before the per-MAC backoff, even with caps on"
        )
        assert scanner.backoff.state(bad_mac) == State.QUARANTINE
        assert any(p["severity"] == "quarantine" for p in fake_ha.posts)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac8_btm_deauth_fallback_is_not_regated_by_cooldown(temp_db_path, fake_ha):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    config = build_config(
        dry_run=False,
        window_samples=1,
        kick_mechanism="btm",
        cooldowns_seconds=[300, 1800],  # a fresh second kick would be blocked by this
    )

    db = Database(temp_db_path)
    await db.connect()
    try:
        fake = FakeController(clients=[_bad(bad_mac, "ap1")])
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)

        await scanner.run_once()  # cycle 1: speculative BTM (a fresh kick)
        assert fake.btm_calls == [(bad_mac, None)]
        assert fake.force_reconnect_calls == []

        await scanner.run_once()  # cycle 2: deauth_fallback on the same AP
        assert fake.force_reconnect_calls == [bad_mac], (
            "AC-8: the deauth_fallback is the same logical kick; the per-MAC cooldown "
            "must not block it"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac8_caps_count_logical_kicks_not_btm_fallback_rows(temp_db_path):
    from wifi_shepard.backoff import evaluate_backoff
    from wifi_shepard.db import Database

    mac = "dc:cc:e6:66:86:2b"
    db = Database(temp_db_path)
    await db.connect()
    try:
        # One logical kick on btm/auto writes two dry_run=0 rows: the fresh BTM and
        # the next-cycle deauth_fallback. The caps must count it as ONE (matching
        # ADR-0004's attempt_group granularity and the in-memory quarantine counter).
        await db.insert_kick(mac=mac, dry_run=False, mechanism="btm")
        await db.insert_kick(mac=mac, dry_run=False, mechanism="deauth_fallback")

        rows = await db.recent_kick_timestamps(mac, since=0.0)
        assert len(rows) == 1, "a BTM+deauth_fallback pair counts as one logical kick"

        # With cap=2 and one logical kick recorded, a second is still allowed.
        allowed, _, _ = evaluate_backoff(
            rows, rows[-1] + 1.0, cooldowns=(), max_per_hour=0, max_per_day=2
        )
        assert allowed is True
    finally:
        await db.close()
