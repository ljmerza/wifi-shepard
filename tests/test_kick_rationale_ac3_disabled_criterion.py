"""ADR-0015 AC-3: a disabled (null) criterion is absent from the rationale.

When signal_dbm_max is null (ADR-0009 disabled) and ap_cu_total_min is 0 (gate
off), the recorded thresholds carry no limit for either and breached omits
'signal' — the rationale reflects only the signals actually tested.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.actor import Actor
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.resolution import resolve_thresholds


@pytest.mark.asyncio
async def test_ac_3_disabled_criterion_omitted_from_rationale(temp_db_path):
    mac = "aa:bb:cc:dd:ee:07"
    config = build_config(
        dry_run=False,
        signal_dbm_max=None,  # ADR-0009: signal criterion disabled
        tx_rate_kbps_max=12000,
        retry_pct_max=30,
        ap_cu_total_min=0,  # saturation gate off
        window_samples=1,
    )
    client = make_client(
        mac=mac,
        signal=-40,  # would NOT breach even if enabled — but it's disabled anyway
        tx_rate_kbps=6000,
        tx_retries=50,
        wifi_tx_attempts=100,
    )

    db = Database(temp_db_path)
    await db.connect()
    try:
        actor = Actor(config=config, controller=FakeController(), db=db)
        await actor.handle(client, resolve_thresholds(mac, config))
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT rationale FROM kick_events WHERE mac = ?", (mac,))
            (raw,) = await cur.fetchone()
    finally:
        await db.close()

    r = json.loads(raw)
    assert "signal_dbm_max" not in r["thresholds"], (
        f"AC-3: a disabled criterion must not appear in thresholds; got {r['thresholds']}"
    )
    assert "ap_cu_total_min" not in r["thresholds"], (
        f"AC-3: an inactive saturation gate (min=0) must not appear; got {r['thresholds']}"
    )
    assert "signal" not in r["breached"], (
        f"AC-3: a disabled criterion must not appear in breached; got {r['breached']}"
    )
    assert set(r["breached"]) == {"tx_rate_kbps", "retry_pct"}, (
        f"AC-3: only the enabled+breached criteria may appear; got {r['breached']}"
    )
