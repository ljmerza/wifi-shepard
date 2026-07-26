"""ADR-0015 AC-7: actor-written dry-run rows are inert to every existing reader.

Running the actor in dry_run mode now persists would-kick rows. Those rows must
be inert: recent_kick_timestamps (backoff/caps), list_devices state/counts, and
overview totals ignore them, while device_summary.dry_run_count reflects them.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.actor import Actor
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.resolution import resolve_thresholds
from wifi_shepard_ui import views

MAC = "aa:bb:cc:dd:ee:07"


@pytest.mark.asyncio
async def test_ac_7_actor_written_dry_run_rows_are_inert(tmp_path):
    now = time.time()
    path = tmp_path / "state.db"
    config = build_config(
        dry_run=True,
        window_samples=1,
        signal_dbm_max=-70,
        tx_rate_kbps_max=12000,
        retry_pct_max=30,
    )
    client = make_client(
        mac=MAC, signal=-85, tx_rate_kbps=4000, tx_retries=60, wifi_tx_attempts=100
    )

    db = Database(path)
    await db.connect()
    try:
        actor = Actor(config=config, controller=FakeController(), db=db)
        await actor.handle(client, resolve_thresholds(MAC, config))

        # Backoff ledger: dry-run rows never count.
        ts = await db.recent_kick_timestamps(MAC, since=0)
        assert ts == [], f"AC-7: dry-run rows must not enter the backoff ledger; got {ts}"
    finally:
        await db.close()

    conn = sqlite3.connect(path)
    try:
        summary = views.device_summary(conn, mac=MAC, allowlist=set(), now=now)
        assert summary.kick_count == 0, "AC-7: dry-run rows must not count as real kicks"
        assert summary.dry_run_count >= 1, (
            "AC-7: device_summary.dry_run_count must reflect the actor-written dry-run row"
        )
        assert summary.state == "NORMAL", "AC-7: a dry-run-only MAC stays NORMAL"

        ov = views.overview(conn, now=now)
        assert ov.kicks_today == 0, "AC-7: overview kicks_today must exclude dry-run rows"

        # list_devices unions real-kick + sample MACs; a dry-run-only MAC must not
        # fabricate a device row (that is exactly the inert guarantee).
        listed = {r.mac for r in views.list_devices(conn, allowlist=set(), now=now)}
        assert MAC not in listed, (
            "AC-7: a dry-run-only MAC must not surface as a real device in list_devices"
        )
    finally:
        conn.close()
