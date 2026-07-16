"""ADR-0012 AC-4: kicks record their trigger in kick_events.trigger.

A kick routed through Actor.handle records the trigger from its context dict —
'dns_thrash' for a DNS flag, 'inactivity' for an ADR-0010 flag, and 'rf' for a
scorer flag (which carries no explicit trigger) — so a DNS-triggered kick is
distinguishable from an RF kick in the database.
"""

from __future__ import annotations

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.actor import Actor
from wifi_shepard.config import build_config
from wifi_shepard.db import Database


@pytest.mark.asyncio
async def test_ac_4_kick_records_trigger(temp_db_path):
    db = Database(temp_db_path)
    await db.connect()
    controller = FakeController()
    # dry_run False + empty cooldowns/caps + no rate limiter => every handle() kicks.
    actor = Actor(config=build_config(dry_run=False), controller=controller, db=db)
    try:
        await actor.handle(make_client(mac="aa:bb:cc:dd:ee:01"), {"trigger": "dns_thrash"})
        await actor.handle(make_client(mac="aa:bb:cc:dd:ee:02"), {"trigger": "inactivity"})
        # Scorer-style decision carries no 'trigger' key -> must default to 'rf'.
        await actor.handle(make_client(mac="aa:bb:cc:dd:ee:03"), {"some": "threshold"})

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT mac, trigger FROM kick_events ORDER BY mac")
            rows = await cur.fetchall()
    finally:
        await db.close()

    triggers = {mac[-2:]: trig for mac, trig in rows}
    assert triggers == {"01": "dns_thrash", "02": "inactivity", "03": "rf"}, (
        f"AC-4: each kick must record its trigger (dns_thrash/inactivity/rf); got {triggers}"
    )
