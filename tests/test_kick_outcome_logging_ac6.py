"""ADR-0003 AC-6: post-kick effectiveness logging.

After a kick at time T, the next scan cycle (T + poll_interval) compares
the polled client's ap_id with the ap_id at the moment of the kick.
- If different (client roamed) → 'kick_succeeded' log line.
- If same (client stayed put) → 'kick_no_roam' log line.

Both log lines must include from_ap, to_ap, mechanism, attempt_group.
"""

from __future__ import annotations

import logging

import pytest

from tests.conftest import FakeController, make_client


def _bad_client(mac: str, ap_id: str = "ap1") -> object:
    return make_client(
        mac=mac,
        signal=-80,
        tx_rate_kbps=4000,
        tx_retries=60,
        wifi_tx_attempts=100,
        radio="ng",
        ap_id=ap_id,
    )


def _healthy_client(mac: str, ap_id: str) -> object:
    """A client present on the controller but not in bad-state — won't be kicked."""
    return make_client(
        mac=mac,
        signal=-50,  # strong signal
        tx_rate_kbps=300_000,  # fast PHY
        tx_retries=1,
        wifi_tx_attempts=100,
        radio="ng",
        ap_id=ap_id,
    )


@pytest.mark.asyncio
async def test_ac_6_kick_succeeded_when_client_roamed_to_different_ap(
    temp_db_path, fake_ha, caplog
):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    config = build_config(dry_run=False, window_samples=1, kick_mechanism="deauth")

    # Cycle 1: bad-state on ap1 → kick fires.
    fake = FakeController(clients=[_bad_client(bad_mac, ap_id="ap1")])
    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        await scanner.run_once()
        assert fake.force_reconnect_calls == [bad_mac]

        # Cycle 2: same MAC reappears on ap2 (roamed), now healthy.
        fake.clients = [_healthy_client(bad_mac, ap_id="ap2")]
        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await scanner.run_once()

        success = [r for r in caplog.records if r.getMessage() == "kick_succeeded"]
        no_roam = [r for r in caplog.records if r.getMessage() == "kick_no_roam"]
        assert len(success) == 1, (
            f"AC-6: expected one kick_succeeded log line; got {len(success)}, "
            f"and {len(no_roam)} kick_no_roam"
        )
        record = success[0]
        assert getattr(record, "mac", None) == bad_mac
        assert getattr(record, "from_ap", None) == "ap1", (
            f"AC-6: from_ap must be the AP at kick time; got {getattr(record, 'from_ap', None)!r}"
        )
        assert getattr(record, "to_ap", None) == "ap2", (
            f"AC-6: to_ap must be the post-kick AP; got {getattr(record, 'to_ap', None)!r}"
        )
        assert getattr(record, "mechanism", None) == "deauth"
        assert getattr(record, "attempt_group", None) is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac_6_kick_no_roam_when_client_stays_on_same_ap(temp_db_path, fake_ha, caplog):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    config = build_config(dry_run=False, window_samples=1, kick_mechanism="deauth")

    # Cycle 1: bad-state on ap1 → kick fires.
    fake = FakeController(clients=[_bad_client(bad_mac, ap_id="ap1")])
    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        await scanner.run_once()
        assert fake.force_reconnect_calls == [bad_mac]

        # Cycle 2: same MAC, same ap1, still bad-state.
        # Note: with kick_mechanism=deauth, the actor will record_kick again on
        # cycle 2 (no fallback path for plain deauth). The kick_no_roam log MUST
        # be emitted before that re-kick fires.
        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await scanner.run_once()

        no_roam = [r for r in caplog.records if r.getMessage() == "kick_no_roam"]
        assert len(no_roam) == 1, f"AC-6: expected one kick_no_roam log line; got {len(no_roam)}"
        record = no_roam[0]
        assert getattr(record, "mac", None) == bad_mac
        assert getattr(record, "from_ap", None) == "ap1"
        assert getattr(record, "to_ap", None) == "ap1"
        assert getattr(record, "mechanism", None) == "deauth"
        assert getattr(record, "attempt_group", None) is not None
    finally:
        await db.close()
