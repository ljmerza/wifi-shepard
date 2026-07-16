"""ADR-0010 AC-7: existing detection is untouched, and the inactivity class has NO
AP-saturation gate — a flag fires even when ap_cu_total is below ap_cu_total_min.

The full pre-existing suite passing is the primary evidence that existing detection
is untouched; here we pin the two claims directly: (1) the conjunctive scorer still
honors the ADR-0008 saturation gate, and (2) the inactivity detector ignores it.
"""

from __future__ import annotations

import logging

import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.scorer import is_bad_state

MAC = "34:ea:e7:11:22:33"


def _below_floor_bad_wifi(**overrides):
    # Weak + slow + retrying (would-be conjunctive bad-state) BUT ap_cu_total=0,
    # far below the 60 saturation floor, with flat byte counters.
    base = dict(
        mac=MAC,
        signal=-85,
        tx_rate_kbps=1000,
        tx_retries=90,
        wifi_tx_attempts=100,
        ap_cu_total=0,
        tx_bytes=4000,
        rx_bytes=1000,
    )
    base.update(overrides)
    return make_client(**base)


def test_conjunctive_scorer_still_gated_by_saturation():
    # Existing behavior intact: with ap_cu_total below the floor, is_bad_state is
    # False even though signal/rate/retry all fail; raise the CU above the floor and
    # the same window flags. (ADR-0008 gate unchanged by this ADR.)
    thresholds = {
        "signal_dbm_max": -70,
        "tx_rate_kbps_max": 12000,
        "retry_pct_max": 30,
        "ap_cu_total_min": 60,
    }
    below = [_below_floor_bad_wifi() for _ in range(3)]
    assert is_bad_state(below, thresholds, ("ng",)) is False, "saturation gate must still spare"
    above = [_below_floor_bad_wifi(ap_cu_total=80) for _ in range(3)]
    assert is_bad_state(above, thresholds, ("ng",)) is True, "conjunctive path still fires"


@pytest.mark.asyncio
async def test_inactivity_flags_below_saturation_floor(temp_db_path, fake_ha, caplog):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    config = build_config(
        dry_run=True,
        window_samples=3,  # conjunctive window; fills within the run
        ap_cu_total_min=60,  # saturation gate ON
        inactivity=dict(enabled=True, min_bytes_per_window=1000, window_samples=3, macs=[MAC]),
    )

    fake = FakeController(clients=[_below_floor_bad_wifi()])
    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            for _ in range(4):  # baseline + 3 flat deltas
                await scanner.run_once()

        would_kick = [r for r in caplog.records if r.getMessage() == "would_kick"]
        # Exactly one would_kick, and it is the inactivity trigger — the conjunctive
        # path was gated off by the saturation floor (ap_cu_total=0 < 60), proving
        # the inactivity class has no such gate.
        assert len(would_kick) == 1, f"expected one would_kick (inactivity only), got {would_kick}"
        assert getattr(would_kick[0], "thresholds", {}).get("trigger") == "inactivity"
        assert fake.force_reconnect_calls == []
    finally:
        await db.close()
