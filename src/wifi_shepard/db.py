from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiosqlite


@runtime_checkable
class Store(Protocol):
    """Persistence surface the scan pipeline depends on.

    Captures exactly what Scanner/Actor need from storage (sample + kick
    writes), so they depend on this abstraction rather than the concrete
    ``Database``. The connect()/close() lifecycle is the composition root's
    concern (main.Daemon) and is intentionally not part of this surface.
    """

    async def insert_sample(self, client: Any) -> None: ...

    async def prune_client_samples(self, *, now: float | None = None) -> int: ...

    async def insert_ap_stats(self, ap: Any) -> None: ...

    async def insert_kick(
        self,
        *,
        mac: str,
        dry_run: bool,
        mechanism: str = "deauth",
        target_bssid: str | None = None,
        attempt_group: str | None = None,
        trigger: str = "rf",
    ) -> None: ...

    async def insert_reboot(
        self,
        *,
        mac: str,
        mode: str,
        outcome: str,
        target: str | None,
        dry_run: bool,
    ) -> None: ...

    async def insert_dns_source_sample(
        self,
        *,
        source_name: str,
        ok: bool,
        query_count: int,
        error: str | None = None,
    ) -> None: ...

    async def insert_dns_thrash_observations(self, rows: list[dict[str, Any]]) -> None: ...

    async def recent_kick_timestamps(self, mac: str, *, since: float) -> list[float]: ...


SCHEMA_CLIENT_SAMPLES = """
CREATE TABLE IF NOT EXISTS client_samples (
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
    name TEXT,
    tx_bytes INTEGER,
    rx_bytes INTEGER
);
"""

# Per-AP health snapshot written each poll cycle: one ap_samples row per AP
# plus one ap_radio_samples row per radio (shared ts). Feeds the read-only UI's
# "noisy APs" view (AP name/MAC, CPU/mem load, per-channel utilization).
SCHEMA_AP_SAMPLES = """
CREATE TABLE IF NOT EXISTS ap_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ap_id TEXT NOT NULL,
    name TEXT,
    mac TEXT,
    cpu_pct REAL,
    mem_pct REAL
);
"""

SCHEMA_AP_RADIO_SAMPLES = """
CREATE TABLE IF NOT EXISTS ap_radio_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ap_id TEXT NOT NULL,
    radio TEXT,
    channel INTEGER,
    cu_total INTEGER
);
"""

SCHEMA_KICK_EVENTS = """
CREATE TABLE IF NOT EXISTS kick_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    mechanism TEXT NOT NULL DEFAULT 'deauth',
    target_bssid TEXT,
    attempt_group TEXT,
    trigger TEXT NOT NULL DEFAULT 'rf'
);
"""

# ADR-0012: DNS observability. dns_source_samples is a per-poll heartbeat (one row
# per Pi-hole instance per cycle) proving the source authenticated and polled;
# dns_thrash_observations snapshots the detector's near-threshold standings so the
# UI can show who is approaching the limit before a kick. Both are display-only —
# neither feeds detection — and both are pruned to a rolling window on write.
SCHEMA_DNS_SOURCE_SAMPLES = """
CREATE TABLE IF NOT EXISTS dns_source_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source_name TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 0,
    query_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
"""

SCHEMA_DNS_THRASH_OBSERVATIONS = """
CREATE TABLE IF NOT EXISTS dns_thrash_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    domain TEXT NOT NULL,
    query_count INTEGER NOT NULL,
    threshold INTEGER NOT NULL,
    over_since REAL
);
"""

# ADR-0012: observability tables are pruned to a rolling window on write so they
# stay observability-sized rather than unbounded like client_samples.
_DNS_RETENTION_SECONDS = 7 * 86400

# ADR-0006: audit trail for every reboot (and every would_reboot). mode is
# 'proactive' | 'reactive'; outcome is 'fired' | 'dry_run'. target is the
# resolved HA entity (or None when unresolved on a dry-run preview).
SCHEMA_REBOOT_EVENTS = """
CREATE TABLE IF NOT EXISTS reboot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mac TEXT NOT NULL,
    mode TEXT NOT NULL,
    outcome TEXT NOT NULL,
    target TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0
);
"""

# Forward-compatible migration: a kick_events table created under ADR-0001 has
# only (id, ts, mac, dry_run). Each ALTER TABLE adds one missing column,
# backfilling existing rows with the default. ADR-0003 AC-8.
_KICK_EVENTS_MIGRATIONS = (
    ("mechanism", "ALTER TABLE kick_events ADD COLUMN mechanism TEXT NOT NULL DEFAULT 'deauth'"),
    ("target_bssid", "ALTER TABLE kick_events ADD COLUMN target_bssid TEXT"),
    ("attempt_group", "ALTER TABLE kick_events ADD COLUMN attempt_group TEXT"),
    # ADR-0012: attribute each kick. Existing rows predate DNS/inactivity kicks and
    # were all RF deauths, so backfilling to 'rf' is accurate for the live ledger.
    ("trigger", "ALTER TABLE kick_events ADD COLUMN trigger TEXT NOT NULL DEFAULT 'rf'"),
)

# Forward-compatible migration: a client_samples table created before the UI
# device-name feature has no `name` column; one created before ADR-0010 has no
# tx_bytes/rx_bytes byte counters. Each ALTER adds one missing column; existing
# rows backfill to NULL.
_CLIENT_SAMPLES_MIGRATIONS = (
    ("name", "ALTER TABLE client_samples ADD COLUMN name TEXT"),
    ("tx_bytes", "ALTER TABLE client_samples ADD COLUMN tx_bytes INTEGER"),
    ("rx_bytes", "ALTER TABLE client_samples ADD COLUMN rx_bytes INTEGER"),
)

# Indexes for the read-only UI's hot query paths. Created idempotently on every
# connect(). ⚠️ The client_samples index MUST declare `mac COLLATE NOCASE`: the
# UI filters with `WHERE mac = ? COLLATE NOCASE`, and SQLite silently ignores a
# BINARY-collation index for a NOCASE predicate (falls back to a full scan). The
# NOCASE index serves both the per-MAC history queries and the overview's
# GROUP BY mac aggregates. The ap_radio_samples index serves the per-radio CU
# sparklines (WHERE ap_id = ? AND radio = ? ORDER BY ts DESC).
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_client_samples_mac_ts "
    "ON client_samples (mac COLLATE NOCASE, ts)",
    "CREATE INDEX IF NOT EXISTS ix_ap_radio_samples_ap_radio_ts "
    "ON ap_radio_samples (ap_id, radio, ts)",
)

# Rolling retention for client_samples, the unbounded per-cycle table (one row
# per client per poll — millions of rows, hundreds of MB). Pruned to a 30-day
# window so it stays query-sized rather than growing forever. Unlike the DNS
# observability tables (pruned on every write), this is pruned opportunistically
# from the scanner loop and throttled: the DELETE predicate is on ts, which the
# (mac, ts) read index does not front, so each prune full-scans. Throttling to
# once an hour keeps that cost negligible without a second ts-leading index.
_CLIENT_SAMPLES_RETENTION_SECONDS = 30 * 86400
_CLIENT_SAMPLES_PRUNE_INTERVAL_SECONDS = 3600


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self.closed = False
        # Last time prune_client_samples() actually ran a DELETE (0.0 = never),
        # used to throttle the prune to once per _CLIENT_SAMPLES_PRUNE_INTERVAL.
        self._last_client_prune = 0.0

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute(SCHEMA_CLIENT_SAMPLES)
        await self._conn.execute(SCHEMA_KICK_EVENTS)
        await self._conn.execute(SCHEMA_REBOOT_EVENTS)
        await self._conn.execute(SCHEMA_AP_SAMPLES)
        await self._conn.execute(SCHEMA_AP_RADIO_SAMPLES)
        await self._conn.execute(SCHEMA_DNS_SOURCE_SAMPLES)
        await self._conn.execute(SCHEMA_DNS_THRASH_OBSERVATIONS)
        await self._migrate_kick_events()
        await self._migrate_client_samples()
        for ddl in _INDEXES:
            await self._conn.execute(ddl)
        await self._conn.commit()

    async def _migrate_kick_events(self) -> None:
        if self._conn is None:
            raise RuntimeError("Database._migrate_kick_events called before connect()")
        cur = await self._conn.execute("PRAGMA table_info(kick_events)")
        existing = {row[1] for row in await cur.fetchall()}
        for column, ddl in _KICK_EVENTS_MIGRATIONS:
            if column not in existing:
                await self._conn.execute(ddl)

    async def _migrate_client_samples(self) -> None:
        if self._conn is None:
            raise RuntimeError("Database._migrate_client_samples called before connect()")
        cur = await self._conn.execute("PRAGMA table_info(client_samples)")
        existing = {row[1] for row in await cur.fetchall()}
        for column, ddl in _CLIENT_SAMPLES_MIGRATIONS:
            if column not in existing:
                await self._conn.execute(ddl)

    async def insert_sample(self, client: Any) -> None:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before insert_sample()")
        await self._conn.execute(
            "INSERT INTO client_samples "
            "(ts, mac, signal, tx_rate_kbps, tx_retries, "
            " wifi_tx_attempts, radio, ap_id, ap_cu_total, name, tx_bytes, rx_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                client.mac,
                client.signal,
                client.tx_rate_kbps,
                client.tx_retries,
                client.wifi_tx_attempts,
                client.radio,
                client.ap_id,
                client.ap_cu_total,
                getattr(client, "name", None),
                getattr(client, "tx_bytes", None),
                getattr(client, "rx_bytes", None),
            ),
        )
        await self._conn.commit()

    async def prune_client_samples(self, *, now: float | None = None) -> int:
        """Delete client_samples older than the 30-day retention window.

        Called once per poll cycle from the scanner, but throttled to at most
        once per _CLIENT_SAMPLES_PRUNE_INTERVAL_SECONDS: the DELETE filters on
        ts (which the (mac, ts) read index does not front), so it full-scans;
        hourly is plenty to hold a rolling window without a second ts index.
        Returns the number of rows deleted (0 when throttled or nothing expired).
        """
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before prune_client_samples()")
        now = time.time() if now is None else now
        if now - self._last_client_prune < _CLIENT_SAMPLES_PRUNE_INTERVAL_SECONDS:
            return 0
        self._last_client_prune = now
        cur = await self._conn.execute(
            "DELETE FROM client_samples WHERE ts < ?",
            (now - _CLIENT_SAMPLES_RETENTION_SECONDS,),
        )
        await self._conn.commit()
        return cur.rowcount

    async def insert_ap_stats(self, ap: Any) -> None:
        """Persist one AP health snapshot: an ap_samples row + one ap_radio_samples
        row per radio, all sharing a single ts so the UI can pair them per poll."""
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before insert_ap_stats()")
        ts = time.time()
        await self._conn.execute(
            "INSERT INTO ap_samples (ts, ap_id, name, mac, cpu_pct, mem_pct) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, ap.id, ap.name, ap.mac, ap.cpu_pct, ap.mem_pct),
        )
        for radio in ap.radios:
            await self._conn.execute(
                "INSERT INTO ap_radio_samples (ts, ap_id, radio, channel, cu_total) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, ap.id, radio.radio, radio.channel, radio.cu_total),
            )
        await self._conn.commit()

    async def insert_kick(
        self,
        *,
        mac: str,
        dry_run: bool,
        mechanism: str = "deauth",
        target_bssid: str | None = None,
        attempt_group: str | None = None,
        trigger: str = "rf",
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before insert_kick()")
        await self._conn.execute(
            "INSERT INTO kick_events "
            "(ts, mac, dry_run, mechanism, target_bssid, attempt_group, trigger) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                mac,
                1 if dry_run else 0,
                mechanism,
                target_bssid,
                attempt_group,
                trigger,
            ),
        )
        await self._conn.commit()

    async def insert_reboot(
        self,
        *,
        mac: str,
        mode: str,
        outcome: str,
        target: str | None,
        dry_run: bool,
    ) -> None:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before insert_reboot()")
        await self._conn.execute(
            "INSERT INTO reboot_events (ts, mac, mode, outcome, target, dry_run) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), mac, mode, outcome, target, 1 if dry_run else 0),
        )
        await self._conn.commit()

    async def insert_dns_source_sample(
        self,
        *,
        source_name: str,
        ok: bool,
        query_count: int,
        error: str | None = None,
    ) -> None:
        """Persist one per-poll DNS-source heartbeat (ADR-0012), pruning old rows."""
        if self._conn is None:
            raise RuntimeError("connect() must be called before insert_dns_source_sample()")
        now = time.time()
        await self._conn.execute(
            "INSERT INTO dns_source_samples (ts, source_name, ok, query_count, error) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, source_name, 1 if ok else 0, int(query_count), error),
        )
        await self._conn.execute(
            "DELETE FROM dns_source_samples WHERE ts < ?", (now - _DNS_RETENTION_SECONDS,)
        )
        await self._conn.commit()

    async def insert_dns_thrash_observations(self, rows: list[dict[str, Any]]) -> None:
        """Persist a batch of near-threshold standings (ADR-0012), pruning old rows.

        Each row is a dict with mac/domain/count/threshold/over_since (the detector's
        ``standings()`` shape). A no-op on an empty batch — a quiet poll writes nothing.
        """
        if self._conn is None:
            raise RuntimeError(
                "Database.connect() must be called before insert_dns_thrash_observations()"
            )
        if not rows:
            return
        now = time.time()
        await self._conn.executemany(
            "INSERT INTO dns_thrash_observations "
            "(ts, mac, domain, query_count, threshold, over_since) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    now,
                    r["mac"],
                    r["domain"],
                    int(r["count"]),
                    int(r["threshold"]),
                    r.get("over_since"),
                )
                for r in rows
            ],
        )
        await self._conn.execute(
            "DELETE FROM dns_thrash_observations WHERE ts < ?", (now - _DNS_RETENTION_SECONDS,)
        )
        await self._conn.commit()

    async def recent_kick_timestamps(self, mac: str, *, since: float) -> list[float]:
        """Logical-kick timestamps for ``mac`` with ts >= ``since``, ascending.

        The source of truth for the ADR-0007 per-MAC cooldown + hourly/daily caps:
        deriving them from this ledger rather than in-memory counters makes the caps
        survive a restart or SIGHUP. Counts one row per *logical* kick — dry-run rows
        are excluded (a dry-run period must not inflate a later live decision), and so
        are ADR-0003 ``deauth_fallback`` rows: a BTM+fallback pair is one logical kick,
        matching ADR-0004's attempt_group granularity and the in-memory quarantine
        counter (which only advances on the fresh path, not the fallback).
        """
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before recent_kick_timestamps()")
        cur = await self._conn.execute(
            "SELECT ts FROM kick_events WHERE mac = ? AND ts >= ? AND dry_run = 0 "
            "AND mechanism != 'deauth_fallback' ORDER BY ts",
            (mac, since),
        )
        rows = await cur.fetchall()
        return [float(row[0]) for row in rows]

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        self.closed = True
