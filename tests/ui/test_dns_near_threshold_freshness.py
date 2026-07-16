"""Regression (self-review): dns_near_threshold must not present stale rows.

The scanner writes observation rows only on a poll with contenders, but writes a
source-health heartbeat every poll. So once the network quiets, the newest
observations are older than the newest heartbeat and must be treated as stale —
not shown as the "current poll."
"""

from __future__ import annotations

import sqlite3

from wifi_shepard_ui import views


def _seed(conn: sqlite3.Connection, *, obs_ts: float, health_ts: float) -> None:
    conn.execute(
        "INSERT INTO dns_thrash_observations "
        "(ts, mac, domain, query_count, threshold, over_since) VALUES (?, ?, ?, ?, ?, ?)",
        (obs_ts, "aa:bb:cc:dd:ee:42", "mqtt-us-4.meross.com", 17, 20, obs_ts - 3600),
    )
    conn.execute(
        "INSERT INTO dns_source_samples (ts, source_name, ok, query_count) VALUES (?, 'gym', 1, 5)",
        (health_ts,),
    )


def test_stale_observations_hidden_when_a_later_quiet_poll_ran(make_db) -> None:
    # Contender observed at t=1000; a later quiet poll wrote a heartbeat at t=2000.
    db_path = make_db(lambda c, now: _seed(c, obs_ts=1000.0, health_ts=2000.0))
    conn = sqlite3.connect(db_path)
    try:
        assert views.dns_near_threshold(conn) == [], (
            "a contender older than the newest heartbeat is a stale poll — must be hidden"
        )
    finally:
        conn.close()


def test_fresh_observations_shown_when_they_are_the_latest(make_db) -> None:
    # Current poll wrote its heartbeat (t=2000) then its observations (t=2001).
    db_path = make_db(lambda c, now: _seed(c, obs_ts=2001.0, health_ts=2000.0))
    conn = sqlite3.connect(db_path)
    try:
        near = views.dns_near_threshold(conn)
        assert len(near) == 1 and near[0].domain == "mqtt-us-4.meross.com", (
            "the current poll's contenders must render"
        )
    finally:
        conn.close()
