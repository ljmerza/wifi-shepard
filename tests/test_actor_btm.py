"""ADR-0003 actor-side mechanism dispatch tests.

This file groups AC-1 (deauth default), AC-2 (explicit btm), AC-3 (auto sends
BTM first), and AC-9 (dry_run logs mechanism but never calls the controller).
AC-4 (BTM-then-deauth_fallback under one attempt_group) lives in its own file
because it requires cross-cycle state.
"""

from __future__ import annotations

import uuid

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client


def _bad_client(mac: str) -> object:
    return make_client(
        mac=mac,
        signal=-80,
        tx_rate_kbps=4000,
        tx_retries=60,
        wifi_tx_attempts=100,
        radio="ng",
    )


@pytest.mark.asyncio
async def test_ac_1_deauth_default_calls_force_reconnect_and_records_deauth_mechanism(
    temp_db_path, fake_ha
):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    fake = FakeController(clients=[_bad_client(bad_mac)])
    config = build_config(dry_run=False, window_samples=1)  # default kick_mechanism="deauth"

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        await scanner.run_once()

        assert fake.force_reconnect_calls == [bad_mac], (
            f"AC-1: deauth default must call force_reconnect_client once; "
            f"got {fake.force_reconnect_calls}"
        )
        assert fake.btm_calls == [], (
            f"AC-1: deauth default must NEVER call send_btm_request; got {fake.btm_calls}"
        )

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mechanism, target_bssid, attempt_group FROM kick_events "
                "WHERE mac = ? AND dry_run = 0",
                (bad_mac,),
            )
            rows = await cur.fetchall()
        assert len(rows) == 1, f"AC-1: expected exactly one real-kick row, got {len(rows)}"
        assert rows[0][0] == "deauth", (
            f"AC-1: kick_events.mechanism must be 'deauth' for default config; got {rows[0][0]!r}"
        )
        assert rows[0][1] is None, (
            f"AC-1: deauth has no target_bssid; got {rows[0][1]!r}"
        )
        # Every kick attempt is a logical group; a deauth-only kick is its own group of one.
        # The UUID lets AC-4's fallback path link a BTM+deauth pair under the same group.
        assert rows[0][2] is not None, (
            "AC-1: kick_events.attempt_group must be set on every real kick (None means the "
            "actor isn't generating attempt_group UUIDs yet)"
        )
        uuid.UUID(rows[0][2])  # raises ValueError if it's not a valid UUID
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac_2_explicit_btm_calls_send_btm_request_and_records_btm_mechanism(
    temp_db_path, fake_ha
):
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    fake = FakeController(clients=[_bad_client(bad_mac)])
    config = build_config(dry_run=False, window_samples=1, kick_mechanism="btm")

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        await scanner.run_once()

        assert fake.btm_calls == [(bad_mac, None)], (
            f"AC-2: kick_mechanism=btm must call send_btm_request(mac, target_bssid=None) "
            f"exactly once; got {fake.btm_calls}"
        )
        assert fake.force_reconnect_calls == [], (
            f"AC-2: kick_mechanism=btm must NOT call force_reconnect_client; "
            f"got {fake.force_reconnect_calls}"
        )

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mechanism, target_bssid, attempt_group FROM kick_events "
                "WHERE mac = ? AND dry_run = 0",
                (bad_mac,),
            )
            rows = await cur.fetchall()
        assert len(rows) == 1, f"AC-2: expected exactly one kick row, got {len(rows)}"
        assert rows[0][0] == "btm", (
            f"AC-2: kick_events.mechanism must be 'btm'; got {rows[0][0]!r}"
        )
        assert rows[0][1] is None, (
            f"AC-2: kick_events.target_bssid must be NULL when no target supplied; "
            f"got {rows[0][1]!r}"
        )
        assert rows[0][2] is not None, "AC-2: attempt_group must be set"
        uuid.UUID(rows[0][2])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac_3_auto_sends_btm_first_no_capability_check_budget_plus_one(
    temp_db_path, fake_ha
):
    """auto-mode is speculative-BTM-then-deauth-fallback. The first cycle always sends BTM,
    regardless of any controller-exposed capability flag (the empirical probe in ADR-0003
    showed UniFi exposes no usable BTM-capability discriminator). The fallback to deauth
    happens on the next cycle (AC-4). The whole pair counts as one logical kick — AC-3
    asserts that the FIRST cycle increments backoff exactly once."""
    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    fake = FakeController(clients=[_bad_client(bad_mac)])
    config = build_config(dry_run=False, window_samples=1, kick_mechanism="auto")

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        await scanner.run_once()

        assert fake.btm_calls == [(bad_mac, None)], (
            f"AC-3: auto-mode must call send_btm_request first; got {fake.btm_calls}"
        )
        assert fake.force_reconnect_calls == [], (
            f"AC-3: auto-mode must NOT call force_reconnect_client on the first cycle "
            f"(deauth fallback only fires on the next cycle, AC-4); "
            f"got {fake.force_reconnect_calls}"
        )

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mechanism FROM kick_events WHERE mac = ? AND dry_run = 0",
                (bad_mac,),
            )
            rows = await cur.fetchall()
        assert len(rows) == 1, (
            f"AC-3: expected exactly one kick row on first cycle, got {len(rows)}"
        )
        assert rows[0][0] == "btm", (
            f"AC-3: auto-mode first attempt records mechanism='btm'; got {rows[0][0]!r}"
        )

        assert scanner.backoff is not None and scanner.backoff.kick_count(bad_mac) == 1, (
            f"AC-3: backoff kick_count must be exactly 1 after one BTM attempt "
            f"(not 2 — the deauth fallback in AC-4 does NOT re-increment); "
            f"got {scanner.backoff.kick_count(bad_mac) if scanner.backoff else None}"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ac_9_dry_run_auto_logs_mechanism_without_calling_controller(
    temp_db_path, fake_ha, caplog
):
    """dry_run preempts every action. Even with kick_mechanism=auto, no BTM and no deauth
    fire — but the would_kick log line must surface the mechanism that *would* have been
    used so operators auditing the dry-run period see 'mechanism=btm' for capable kicks
    before they ever flip dry_run=false."""
    import logging

    from wifi_shepard.config import build_config
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    bad_mac = "dc:cc:e6:66:86:2b"
    fake = FakeController(clients=[_bad_client(bad_mac)])
    config = build_config(dry_run=True, window_samples=1, kick_mechanism="auto")

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(controller=fake, db=db, config=config, ha=fake_ha)
        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await scanner.run_once()

        assert fake.btm_calls == [], (
            f"AC-9: dry_run must not call send_btm_request; got {fake.btm_calls}"
        )
        assert fake.force_reconnect_calls == [], (
            f"AC-9: dry_run must not call force_reconnect_client; "
            f"got {fake.force_reconnect_calls}"
        )

        would_kick_records = [r for r in caplog.records if r.getMessage() == "would_kick"]
        assert len(would_kick_records) == 1, (
            f"AC-9: expected exactly one would_kick log line, got {len(would_kick_records)}"
        )
        record = would_kick_records[0]
        assert getattr(record, "mac", None) == bad_mac, (
            f"AC-9: would_kick must include mac field; got {getattr(record, 'mac', None)!r}"
        )
        assert getattr(record, "mechanism", None) == "btm", (
            f"AC-9: would_kick under kick_mechanism=auto must record mechanism='btm' "
            f"(auto resolves to BTM-first speculative); got "
            f"{getattr(record, 'mechanism', None)!r}"
        )
    finally:
        await db.close()
