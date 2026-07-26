"""ADR-0015 AC-2: a live RF kick persists a full rationale JSON.

The kick_events.rationale of an RF kick records the envelope (v, trigger,
window_samples, quiet_hours, override), the witness sample's observed values,
the thresholds in force, and a breached list naming exactly the active criteria.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.actor import Actor
from wifi_shepard.config import build_config
from wifi_shepard.resolution import resolve_thresholds


async def _kick_and_read_rationale(temp_db_path, config, client, mac):
    from wifi_shepard.db import Database

    db = Database(temp_db_path)
    await db.connect()
    try:
        actor = Actor(config=config, controller=FakeController(), db=db)
        ctx = resolve_thresholds(mac, config)  # the scorer's RF decision dict
        await actor.handle(client, ctx)
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT rationale FROM kick_events WHERE mac = ? ORDER BY ts DESC LIMIT 1",
                (mac,),
            )
            (raw,) = await cur.fetchone()
    finally:
        await db.close()
    assert raw is not None, "AC-2: a live kick must persist a non-null rationale"
    return json.loads(raw)


@pytest.mark.asyncio
async def test_ac_2_live_rf_kick_records_full_rationale(temp_db_path):
    mac = "dc:cc:e6:66:86:2b"
    config = build_config(
        dry_run=False,
        signal_dbm_max=-70,
        tx_rate_kbps_max=12000,
        retry_pct_max=30,
        ap_cu_total_min=60,
        window_samples=1,
    )
    client = make_client(
        mac=mac,
        signal=-78,
        tx_rate_kbps=6000,
        tx_retries=41,
        wifi_tx_attempts=100,
        radio="ng",
        ap_cu_total=74,
    )

    r = await _kick_and_read_rationale(temp_db_path, config, client, mac)

    assert r["v"] == 1, f"AC-2: envelope must carry a version, got {r.get('v')!r}"
    assert r["trigger"] == "rf"
    assert r["window_samples"] == 1
    assert r["quiet_hours"] is False
    assert r["override"] is False

    observed = r["observed"]
    assert observed["signal"] == -78
    assert observed["tx_rate_kbps"] == 6000
    assert observed["retry_pct"] == 41.0
    assert observed["radio"] == "ng"
    assert observed["ap_cu_total"] == 74

    thresholds = r["thresholds"]
    assert thresholds["signal_dbm_max"] == -70
    assert thresholds["tx_rate_kbps_max"] == 12000
    assert thresholds["retry_pct_max"] == 30
    assert thresholds["ap_cu_total_min"] == 60

    assert set(r["breached"]) == {"signal", "tx_rate_kbps", "retry_pct"}, (
        f"AC-2: breached must name exactly the active criteria; got {r['breached']}"
    )


@pytest.mark.asyncio
async def test_ac_2_override_flag_true_when_override_matches(temp_db_path):
    """The override flag reflects whether an overrides: entry matched this MAC."""
    mac = "dc:cc:e6:66:86:2b"
    config = build_config(
        dry_run=False,
        signal_dbm_max=-70,
        tx_rate_kbps_max=12000,
        retry_pct_max=30,
        window_samples=1,
        overrides=[{"mac": mac, "signal_dbm_max": -65}],
    )
    client = make_client(
        mac=mac, signal=-78, tx_rate_kbps=6000, tx_retries=41, wifi_tx_attempts=100
    )
    r = await _kick_and_read_rationale(temp_db_path, config, client, mac)
    assert r["override"] is True, (
        "AC-2: override flag must be True when an override matches the MAC"
    )
