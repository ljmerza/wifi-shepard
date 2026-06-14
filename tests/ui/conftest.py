from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest

DAEMON_SCHEMA = """
CREATE TABLE client_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    signal INTEGER,
    tx_rate_kbps INTEGER,
    tx_retries INTEGER,
    wifi_tx_attempts INTEGER,
    radio TEXT,
    ap_id TEXT,
    ap_cu_total INTEGER,
    name TEXT
);
CREATE TABLE kick_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    mechanism TEXT NOT NULL DEFAULT 'deauth',
    target_bssid TEXT,
    attempt_group TEXT
);
CREATE TABLE ap_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ap_id TEXT NOT NULL,
    name TEXT,
    mac TEXT,
    cpu_pct REAL,
    mem_pct REAL
);
CREATE TABLE ap_radio_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ap_id TEXT NOT NULL,
    radio TEXT,
    channel INTEGER,
    cu_total INTEGER
);
"""

MAC_A = "AA:BB:CC:DD:EE:FF"
MAC_B = "11:22:33:44:55:66"


def _seed_default(conn: sqlite3.Connection, now: float) -> None:
    """Insert a useful baseline: MAC_A has named samples + a dry-run kick + a real
    kick; MAC_B has one unnamed healthy sample and zero kicks; two APs reported."""
    for offset, signal in [(180, -72), (120, -75), (60, -78)]:
        conn.execute(
            "INSERT INTO client_samples "
            "(ts, mac, signal, tx_rate_kbps, tx_retries, "
            " wifi_tx_attempts, radio, ap_id, ap_cu_total, name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now - offset, MAC_A, signal, 6000, 50, 100, "ng", "ap1", 70, "wled-kitchen"),
        )
    # one dry-run, one real kick
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 1)",
        (now - 150, MAC_A),
    )
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 0)",
        (now - 90, MAC_A),
    )
    # MAC_B: healthy, no kicks, no reported name (renders as "—")
    conn.execute(
        "INSERT INTO client_samples "
        "(ts, mac, signal, tx_rate_kbps, tx_retries, "
        " wifi_tx_attempts, radio, ap_id, ap_cu_total, name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now - 30, MAC_B, -55, 144000, 5, 100, "na", "ap2", 30, None),
    )
    # Two APs, each with two radios. ap1 is the noisier (peak cu 72 > ap2's 40).
    _seed_ap(
        conn,
        now,
        ap_id="ap1",
        name="Front Porch",
        mac="ff:ee:dd:cc:bb:aa",
        cpu=6.0,
        mem=42.0,
        radios=[("ng", 6, 72), ("na", 36, 35)],
    )
    _seed_ap(
        conn,
        now,
        ap_id="ap2",
        name="Garage",
        mac="11:22:33:44:55:66",
        cpu=11.0,
        mem=51.0,
        radios=[("ng", 1, 40), ("na", 149, 22)],
    )


def _seed_ap(
    conn: sqlite3.Connection,
    now: float,
    *,
    ap_id: str,
    name: str,
    mac: str,
    cpu: float,
    mem: float,
    radios: list[tuple[str, int, int]],
) -> None:
    conn.execute(
        "INSERT INTO ap_samples (ts, ap_id, name, mac, cpu_pct, mem_pct) VALUES (?, ?, ?, ?, ?, ?)",
        (now, ap_id, name, mac, cpu, mem),
    )
    for radio, channel, cu in radios:
        conn.execute(
            "INSERT INTO ap_radio_samples (ts, ap_id, radio, channel, cu_total) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, ap_id, radio, channel, cu),
        )


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(DAEMON_SCHEMA)
    _seed_default(conn, time.time())
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def make_db(tmp_path: Path) -> Callable[..., Path]:
    """Factory for tests that need a custom-seeded DB."""

    def _make(seed: Callable[[sqlite3.Connection, float], None] | None = None) -> Path:
        db_path = tmp_path / f"state_{int(time.time() * 1000)}.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(DAEMON_SCHEMA)
        if seed is not None:
            seed(conn, time.time())
        conn.commit()
        conn.close()
        return db_path

    return _make
