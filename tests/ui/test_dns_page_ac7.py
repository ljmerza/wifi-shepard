"""ADR-0012 AC-7: GET /dns renders source health, near-threshold, and DNS kicks.

The page reads dns_source_samples (per-instance health + volume),
dns_thrash_observations (who's approaching the threshold), and
kick_events WHERE trigger='dns_thrash' (what DNS actually kicked).
"""

from __future__ import annotations

import sqlite3


def _seed_dns(conn: sqlite3.Connection, now: float) -> None:
    # Two source-health rows: gym healthy (1200 q), bonus healthy (980 q).
    conn.execute(
        "INSERT INTO dns_source_samples (ts, source_name, ok, query_count, error) "
        "VALUES (?, 'gym', 1, 1200, NULL)",
        (now - 20,),
    )
    conn.execute(
        "INSERT INTO dns_source_samples (ts, source_name, ok, query_count, error) "
        "VALUES (?, 'bonus', 1, 980, NULL)",
        (now - 21,),
    )
    # A near-threshold contender: 17 of 20, sustained ~1h.
    conn.execute(
        "INSERT INTO client_samples (ts, mac, name) VALUES (?, ?, ?)",
        (now - 30, "aa:bb:cc:dd:ee:42", "meross-plug-den"),
    )
    conn.execute(
        "INSERT INTO dns_thrash_observations "
        "(ts, mac, domain, query_count, threshold, over_since) VALUES (?, ?, ?, ?, ?, ?)",
        (now - 15, "aa:bb:cc:dd:ee:42", "mqtt-us-4.meross.com", 17, 20, now - 3600),
    )
    # A real kick that DNS triggered.
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run, mechanism, trigger) "
        "VALUES (?, ?, 0, 'deauth', 'dns_thrash')",
        (now - 60, "aa:bb:cc:dd:ee:42"),
    )


def test_ac_7_dns_page_shows_health_thrashers_and_kicks(make_db) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    db_path = make_db(_seed_dns)
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        response = client.get("/dns")

    assert response.status_code == 200, "GET /dns must render"
    text = response.text
    lower = text.lower()

    # Source health — both instances surface with their query volume.
    assert "gym" in lower and "bonus" in lower, "both Pi-hole instances must appear"
    assert "1200" in text or "1,200" in text, "gym's query volume must render"

    # Near-threshold — the domain, the MAC's name, and the count/threshold.
    assert "mqtt-us-4.meross.com" in lower, "the near-threshold domain must render"
    assert "meross-plug-den" in lower, "the offending device's name must render"
    assert "17" in text and "20" in text, "the count/threshold (17/20) must render"

    # DNS-triggered kicks — the trigger surfaces so DNS kicks are attributable.
    assert "dns_thrash" in lower or "dns thrash" in lower, "DNS-triggered kicks must surface"
