"""ADR-0015 AC-8: both kick paths log their rationale.

A live kick emits a `kick` log record and a dry-run emits `would_kick`, each
carrying the same rationale payload that is persisted to the row — closing the
asymmetry where a live kick logged nothing.
"""

from __future__ import annotations

import json
import logging

import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.actor import Actor
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.resolution import resolve_thresholds


def _rationale_of(record):
    payload = getattr(record, "rationale", None)
    if payload is None:
        return None
    if isinstance(payload, str):
        return json.loads(payload)
    return payload


@pytest.mark.asyncio
async def test_ac_8_live_kick_logs_rationale(temp_db_path, caplog):
    mac = "aa:bb:cc:dd:ee:08"
    config = build_config(
        dry_run=False, window_samples=1, signal_dbm_max=-70,
        tx_rate_kbps_max=12000, retry_pct_max=30,
    )
    client = make_client(mac=mac, signal=-85, tx_rate_kbps=4000,
                         tx_retries=60, wifi_tx_attempts=100)
    db = Database(temp_db_path)
    await db.connect()
    try:
        actor = Actor(config=config, controller=FakeController(), db=db)
        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await actor.handle(client, resolve_thresholds(mac, config))
    finally:
        await db.close()

    kicks = [r for r in caplog.records if r.getMessage() == "kick"
             and getattr(r, "mac", None) == mac]
    assert len(kicks) == 1, (
        f"AC-8: a live kick must emit exactly one 'kick' log line; "
        f"got {[r.getMessage() for r in caplog.records]}"
    )
    rationale = _rationale_of(kicks[0])
    assert rationale is not None and rationale.get("trigger") == "rf", (
        f"AC-8: the 'kick' log must carry the rationale payload; got {rationale!r}"
    )


@pytest.mark.asyncio
async def test_ac_8_dry_run_logs_rationale(temp_db_path, caplog):
    mac = "aa:bb:cc:dd:ee:18"
    config = build_config(
        dry_run=True, window_samples=1, signal_dbm_max=-70,
        tx_rate_kbps_max=12000, retry_pct_max=30,
    )
    client = make_client(mac=mac, signal=-85, tx_rate_kbps=4000,
                         tx_retries=60, wifi_tx_attempts=100)
    db = Database(temp_db_path)
    await db.connect()
    try:
        actor = Actor(config=config, controller=FakeController(), db=db)
        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await actor.handle(client, resolve_thresholds(mac, config))
    finally:
        await db.close()

    would = [r for r in caplog.records if r.getMessage() == "would_kick"
             and getattr(r, "mac", None) == mac]
    assert len(would) == 1, "AC-8: a dry-run must emit exactly one 'would_kick' log line"
    rationale = _rationale_of(would[0])
    assert rationale is not None and rationale.get("trigger") == "rf", (
        f"AC-8: the 'would_kick' log must carry the rationale payload; got {rationale!r}"
    )
