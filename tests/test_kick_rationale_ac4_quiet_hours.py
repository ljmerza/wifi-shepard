"""ADR-0015 AC-4: quiet hours is flagged by the scorer and recorded truthfully.

Inside quiet hours the scorer stamps quiet_hours=true into its decision and
tightens the thresholds; a kick built from that decision records quiet_hours
true and the tightened limits. Outside quiet hours it records quiet_hours false
and the untightened limits.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.actor import Actor
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.scorer import Scorer

# 02:00 UTC is inside the 23:00–07:00 window; 12:00 UTC is outside.
INSIDE = datetime(2026, 1, 1, 2, 0, tzinfo=UTC).timestamp()
OUTSIDE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC).timestamp()


def _config():
    return build_config(
        dry_run=False,
        window_samples=1,
        signal_dbm_max=-70,
        tx_rate_kbps_max=12000,
        retry_pct_max=30,
        quiet_hours={
            "start": "23:00",
            "end": "07:00",
            "timezone": "UTC",
            # Stricter: tx rate floor lower (8000), retry ceiling higher (40).
            "override_threshold": {"tx_rate_kbps_max": 8000, "retry_pct_max": 40},
        },
    )


def _bad_client(mac):
    # Bad even under the tightened thresholds: tx 4000<8000, retry 60>40, signal -85<-70.
    return make_client(mac=mac, signal=-85, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100)


@pytest.mark.asyncio
async def test_ac_4_inside_quiet_hours_records_tightened_limits(temp_db_path):
    config = _config()
    mac = "aa:bb:cc:dd:ee:04"

    scorer = Scorer(config, wall_now_fn=lambda: INSIDE)
    decision = scorer.ingest(_bad_client(mac))
    assert decision is not None, "precondition: the client is bad inside quiet hours"
    assert decision.get("quiet_hours") is True, (
        f"AC-4: the scorer must stamp quiet_hours=True inside the window; got {decision}"
    )
    assert decision["tx_rate_kbps_max"] == 8000, "AC-4: decision must carry the tightened tx floor"

    db = Database(temp_db_path)
    await db.connect()
    try:
        actor = Actor(config=config, controller=FakeController(), db=db)
        await actor.handle(_bad_client(mac), decision)
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT rationale FROM kick_events WHERE mac = ?", (mac,))
            (raw,) = await cur.fetchone()
    finally:
        await db.close()

    r = json.loads(raw)
    assert r["quiet_hours"] is True, (
        "AC-4: rationale must record quiet_hours=True inside the window"
    )
    assert r["thresholds"]["tx_rate_kbps_max"] == 8000, (
        f"AC-4: rationale must record the TIGHTENED tx floor; got {r['thresholds']}"
    )


@pytest.mark.asyncio
async def test_ac_4_outside_quiet_hours_records_untightened_limits(temp_db_path):
    config = _config()
    mac = "aa:bb:cc:dd:ee:05"

    scorer = Scorer(config, wall_now_fn=lambda: OUTSIDE)
    decision = scorer.ingest(_bad_client(mac))
    assert decision is not None
    assert not decision.get("quiet_hours"), (
        f"AC-4: outside the window the decision must not claim quiet_hours; got {decision}"
    )

    db = Database(temp_db_path)
    await db.connect()
    try:
        actor = Actor(config=config, controller=FakeController(), db=db)
        await actor.handle(_bad_client(mac), decision)
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT rationale FROM kick_events WHERE mac = ?", (mac,))
            (raw,) = await cur.fetchone()
    finally:
        await db.close()

    r = json.loads(raw)
    assert r["quiet_hours"] is False, "AC-4: rationale must record quiet_hours=False outside"
    assert r["thresholds"]["tx_rate_kbps_max"] == 12000, (
        f"AC-4: rationale must record the UNTIGHTENED tx floor outside; got {r['thresholds']}"
    )
