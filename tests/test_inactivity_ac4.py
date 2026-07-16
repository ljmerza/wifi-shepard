"""ADR-0010 AC-4: an opted-in MAC whose summed byte delta over a full window is
below the floor is flagged and reaches Actor.handle; under dry_run:true this
produces the would-kick log path with trigger=inactivity and no controller call.
"""

from __future__ import annotations

import logging

import pytest

from tests.conftest import FakeController, make_client

MAC = "34:ea:e7:11:22:33"


def _wedged(**overrides):
    # Strong-signal, fast, low-retry — the conjunctive scorer would SPARE this
    # (that's the whole blind spot). Flat byte counters make it a flatline.
    base = dict(
        mac=MAC,
        signal=-48,
        tx_rate_kbps=60000,
        tx_retries=0,
        wifi_tx_attempts=1000,
        ap_cu_total=70,
        tx_bytes=5000,
        rx_bytes=9000,
    )
    base.update(overrides)
    return make_client(**base)


@pytest.mark.asyncio
async def test_flatline_reaches_actor_would_kick_no_controller_call(temp_db_path, fake_ha, caplog):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    config = build_config(
        dry_run=True,
        window_samples=5,  # conjunctive scorer window (unrelated to inactivity's)
        inactivity=dict(
            enabled=True,
            min_bytes_per_window=1000,
            window_samples=3,
            macs=[MAC],
        ),
    )

    fake = FakeController(clients=[_wedged()])
    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)

        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            # prev-baseline poll + 3 flat deltas → window fills on the 4th poll.
            for _ in range(4):
                await scanner.run_once()

        would_kick = [r for r in caplog.records if r.getMessage() == "would_kick"]
        assert len(would_kick) == 1, f"expected exactly one would_kick, got {len(would_kick)}"
        rec = would_kick[0]
        assert getattr(rec, "mac", None) == MAC
        thresholds = getattr(rec, "thresholds", {})
        assert thresholds.get("trigger") == "inactivity", (
            f"would_kick must identify the inactivity trigger; got {thresholds!r}"
        )
        assert thresholds.get("window_bytes") == 0

        # dry_run → no controller call, no HA notification.
        assert fake.force_reconnect_calls == []
        assert fake.btm_calls == []
        assert fake_ha.posts == []
    finally:
        await db.close()
