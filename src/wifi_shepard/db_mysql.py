"""MySQL/MariaDB persistence backend, selected via ``WIFI_SHEPARD_DB_URL``.

An alternative to the default SQLite ``Database`` for deployments that already
run a MariaDB/MySQL server. ``create_database`` in ``db.py`` picks this class
when a ``mysql://`` (or ``mariadb://``) URL is configured; it implements the
same ``Store`` surface plus the connect()/close() lifecycle, so the daemon's
composition root treats the two backends interchangeably.

Schema parity notes vs. ``db.py`` (the canonical schema reference):
- ``signal`` and ``trigger`` are reserved words in MySQL/MariaDB, so every
  statement touching those columns backticks them.
- SQLite's ``COLLATE NOCASE`` index has no equivalent here and needs none: the
  tables are created under the connection's utf8mb4 case-insensitive collation,
  so plain equality (and the plain (mac, ts) index) already serves the UI's
  case-insensitive MAC lookups.
- Timestamps stay epoch floats (``DOUBLE``), matching what every reader parses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import aiomysql

from .db import (
    _CLIENT_SAMPLES_PRUNE_INTERVAL_SECONDS,
    _CLIENT_SAMPLES_RETENTION_SECONDS,
    _DNS_RETENTION_SECONDS,
)

_URL_SCHEMES = ("mysql", "mariadb")
_URL_SHAPE = "mysql://user:password@host[:3306]/database"


@dataclass(frozen=True)
class MySQLTarget:
    """Connection coordinates parsed (fail-closed) from a WIFI_SHEPARD_DB_URL."""

    host: str
    port: int
    user: str
    password: str
    database: str


def parse_db_url(url: str) -> MySQLTarget:
    """Parse a ``mysql://user:password@host:port/database`` URL, fail-closed.

    Every component except the port is required; a URL that omits one gets a
    clear error naming the missing piece rather than a half-configured backend
    (PLAN.md §5 fail-closed rule). Percent-encoding in the user/password is
    decoded, so passwords containing ``@``/``/``/``:`` work when encoded.
    """
    split = urlsplit(url)
    if split.scheme not in _URL_SCHEMES:
        raise ValueError(f"unsupported db url scheme {split.scheme!r}: expected {_URL_SHAPE}")
    if not split.hostname:
        raise ValueError(f"db url is missing a host: expected {_URL_SHAPE}")
    if not split.username:
        raise ValueError(f"db url is missing a username: expected {_URL_SHAPE}")
    if split.password is None or split.password == "":
        raise ValueError(f"db url is missing a password: expected {_URL_SHAPE}")
    database = split.path.lstrip("/")
    if not database or "/" in database:
        raise ValueError(f"db url is missing a database name: expected {_URL_SHAPE}")
    return MySQLTarget(
        host=split.hostname,
        port=split.port or 3306,
        user=unquote(split.username),
        password=unquote(split.password),
        database=unquote(database),
    )


SCHEMA_CLIENT_SAMPLES = """
CREATE TABLE IF NOT EXISTS client_samples (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ts DOUBLE NOT NULL,
    mac VARCHAR(64) NOT NULL,
    `signal` INT,
    tx_rate_kbps BIGINT,
    tx_retries BIGINT,
    wifi_tx_attempts BIGINT,
    radio VARCHAR(32),
    ap_id VARCHAR(64),
    ap_cu_total INT,
    name VARCHAR(255),
    tx_bytes BIGINT,
    rx_bytes BIGINT
)
"""

SCHEMA_AP_SAMPLES = """
CREATE TABLE IF NOT EXISTS ap_samples (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ts DOUBLE NOT NULL,
    ap_id VARCHAR(64) NOT NULL,
    name VARCHAR(255),
    mac VARCHAR(64),
    cpu_pct DOUBLE,
    mem_pct DOUBLE
)
"""

SCHEMA_AP_RADIO_SAMPLES = """
CREATE TABLE IF NOT EXISTS ap_radio_samples (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ts DOUBLE NOT NULL,
    ap_id VARCHAR(64) NOT NULL,
    radio VARCHAR(32),
    channel INT,
    cu_total INT
)
"""

SCHEMA_KICK_EVENTS = """
CREATE TABLE IF NOT EXISTS kick_events (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ts DOUBLE NOT NULL,
    mac VARCHAR(64) NOT NULL,
    dry_run TINYINT NOT NULL DEFAULT 0,
    mechanism VARCHAR(32) NOT NULL DEFAULT 'deauth',
    target_bssid VARCHAR(64),
    attempt_group VARCHAR(64),
    `trigger` VARCHAR(32) NOT NULL DEFAULT 'rf',
    rationale TEXT
)
"""

SCHEMA_REBOOT_EVENTS = """
CREATE TABLE IF NOT EXISTS reboot_events (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ts DOUBLE NOT NULL,
    mac VARCHAR(64) NOT NULL,
    mode VARCHAR(16) NOT NULL,
    outcome VARCHAR(16) NOT NULL,
    target VARCHAR(255),
    dry_run TINYINT NOT NULL DEFAULT 0
)
"""

SCHEMA_DNS_SOURCE_SAMPLES = """
CREATE TABLE IF NOT EXISTS dns_source_samples (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ts DOUBLE NOT NULL,
    source_name VARCHAR(255) NOT NULL,
    ok TINYINT NOT NULL DEFAULT 0,
    query_count BIGINT NOT NULL DEFAULT 0,
    error TEXT
)
"""

SCHEMA_DNS_THRASH_OBSERVATIONS = """
CREATE TABLE IF NOT EXISTS dns_thrash_observations (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ts DOUBLE NOT NULL,
    mac VARCHAR(64) NOT NULL,
    domain VARCHAR(255) NOT NULL,
    query_count BIGINT NOT NULL,
    threshold BIGINT NOT NULL,
    over_since DOUBLE
)
"""

_ALL_SCHEMAS = (
    SCHEMA_CLIENT_SAMPLES,
    SCHEMA_KICK_EVENTS,
    SCHEMA_REBOOT_EVENTS,
    SCHEMA_AP_SAMPLES,
    SCHEMA_AP_RADIO_SAMPLES,
    SCHEMA_DNS_SOURCE_SAMPLES,
    SCHEMA_DNS_THRASH_OBSERVATIONS,
)

# Same forward-compat column adds as db.py's _KICK_EVENTS_MIGRATIONS /
# _CLIENT_SAMPLES_MIGRATIONS, in MySQL types. On a fresh database the CREATEs
# above already carry every column and these are no-ops; they only matter for a
# database created by an older daemon build.
_KICK_EVENTS_MIGRATIONS = (
    (
        "mechanism",
        "ALTER TABLE kick_events ADD COLUMN mechanism VARCHAR(32) NOT NULL DEFAULT 'deauth'",
    ),
    ("target_bssid", "ALTER TABLE kick_events ADD COLUMN target_bssid VARCHAR(64)"),
    ("attempt_group", "ALTER TABLE kick_events ADD COLUMN attempt_group VARCHAR(64)"),
    ("trigger", "ALTER TABLE kick_events ADD COLUMN `trigger` VARCHAR(32) NOT NULL DEFAULT 'rf'"),
    # ADR-0015: nullable per-kick rationale snapshot (JSON text).
    ("rationale", "ALTER TABLE kick_events ADD COLUMN rationale TEXT"),
)

_CLIENT_SAMPLES_MIGRATIONS = (
    ("name", "ALTER TABLE client_samples ADD COLUMN name VARCHAR(255)"),
    ("tx_bytes", "ALTER TABLE client_samples ADD COLUMN tx_bytes BIGINT"),
    ("rx_bytes", "ALTER TABLE client_samples ADD COLUMN rx_bytes BIGINT"),
)

# Same hot-path indexes as db.py's _INDEXES. Existence is checked through
# information_schema (rather than MariaDB's CREATE INDEX IF NOT EXISTS
# extension) so this also runs on stock MySQL.
_INDEXES = (
    (
        "client_samples",
        "ix_client_samples_mac_ts",
        "CREATE INDEX ix_client_samples_mac_ts ON client_samples (mac, ts)",
    ),
    (
        "ap_radio_samples",
        "ix_ap_radio_samples_ap_radio_ts",
        "CREATE INDEX ix_ap_radio_samples_ap_radio_ts ON ap_radio_samples (ap_id, radio, ts)",
    ),
)


class MySQLDatabase:
    """``Store`` implementation over aiomysql, mirroring ``Database`` in db.py.

    Connections come from a small pool with ``pool_recycle`` so a MariaDB
    restart or idle-timeout gives the next write a fresh connection instead of
    a dead one; the scanner's per-cycle exception guard absorbs the one failed
    cycle in between. Statements run with autocommit — every insert here is a
    single-statement transaction, matching the per-call commit() granularity of
    the SQLite backend (insert_ap_stats keeps its multi-row atomicity with an
    explicit transaction).
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.target = parse_db_url(url)
        self._pool: aiomysql.Pool | None = None
        self.closed = False
        # Same prune throttle bookkeeping as the SQLite backend.
        self._last_client_prune = 0.0

    async def connect(self) -> None:
        self._pool = await aiomysql.create_pool(
            host=self.target.host,
            port=self.target.port,
            user=self.target.user,
            password=self.target.password,
            db=self.target.database,
            charset="utf8mb4",
            autocommit=True,
            minsize=1,
            maxsize=2,
            pool_recycle=3600,
        )
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            for ddl in _ALL_SCHEMAS:
                await cur.execute(ddl)
            await self._migrate(cur, "kick_events", _KICK_EVENTS_MIGRATIONS)
            await self._migrate(cur, "client_samples", _CLIENT_SAMPLES_MIGRATIONS)
            for table, index_name, ddl in _INDEXES:
                await cur.execute(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
                    (table, index_name),
                )
                (count,) = await cur.fetchone()
                if not count:
                    await cur.execute(ddl)

    async def _migrate(self, cur: Any, table: str, migrations: tuple[tuple[str, str], ...]) -> None:
        await cur.execute(f"SHOW COLUMNS FROM {table}")
        existing = {row[0] for row in await cur.fetchall()}
        for column, ddl in migrations:
            if column not in existing:
                await cur.execute(ddl)

    def _require_pool(self, method: str) -> aiomysql.Pool:
        if self._pool is None:
            raise RuntimeError(f"MySQLDatabase.connect() must be called before {method}()")
        return self._pool

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        pool = self._require_pool("_execute")
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount

    async def insert_sample(self, client: Any) -> None:
        await self._execute(
            "INSERT INTO client_samples "
            "(ts, mac, `signal`, tx_rate_kbps, tx_retries, "
            " wifi_tx_attempts, radio, ap_id, ap_cu_total, name, tx_bytes, rx_bytes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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

    async def prune_client_samples(self, *, now: float | None = None) -> int:
        """30-day retention DELETE, throttled to once an hour — see db.py."""
        self._require_pool("prune_client_samples")
        now = time.time() if now is None else now
        if now - self._last_client_prune < _CLIENT_SAMPLES_PRUNE_INTERVAL_SECONDS:
            return 0
        self._last_client_prune = now
        return await self._execute(
            "DELETE FROM client_samples WHERE ts < %s",
            (now - _CLIENT_SAMPLES_RETENTION_SECONDS,),
        )

    async def insert_ap_stats(self, ap: Any) -> None:
        """One ap_samples row + one ap_radio_samples row per radio, sharing a ts.

        Wrapped in a transaction so the UI never sees an AP snapshot with only
        half its radios — the same atomicity the SQLite backend gets from its
        single trailing commit().
        """
        pool = self._require_pool("insert_ap_stats")
        ts = time.time()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO ap_samples (ts, ap_id, name, mac, cpu_pct, mem_pct) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (ts, ap.id, ap.name, ap.mac, ap.cpu_pct, ap.mem_pct),
                    )
                    for radio in ap.radios:
                        await cur.execute(
                            "INSERT INTO ap_radio_samples (ts, ap_id, radio, channel, cu_total) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            (ts, ap.id, radio.radio, radio.channel, radio.cu_total),
                        )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def insert_kick(
        self,
        *,
        mac: str,
        dry_run: bool,
        mechanism: str = "deauth",
        target_bssid: str | None = None,
        attempt_group: str | None = None,
        trigger: str = "rf",
        rationale: str | None = None,
    ) -> None:
        await self._execute(
            "INSERT INTO kick_events "
            "(ts, mac, dry_run, mechanism, target_bssid, attempt_group, `trigger`, rationale) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                time.time(),
                mac,
                1 if dry_run else 0,
                mechanism,
                target_bssid,
                attempt_group,
                trigger,
                rationale,
            ),
        )

    async def insert_reboot(
        self,
        *,
        mac: str,
        mode: str,
        outcome: str,
        target: str | None,
        dry_run: bool,
    ) -> None:
        await self._execute(
            "INSERT INTO reboot_events (ts, mac, mode, outcome, target, dry_run) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (time.time(), mac, mode, outcome, target, 1 if dry_run else 0),
        )

    async def insert_dns_source_sample(
        self,
        *,
        source_name: str,
        ok: bool,
        query_count: int,
        error: str | None = None,
    ) -> None:
        """Per-poll DNS-source heartbeat (ADR-0012), pruned on write like db.py."""
        now = time.time()
        await self._execute(
            "INSERT INTO dns_source_samples (ts, source_name, ok, query_count, error) "
            "VALUES (%s, %s, %s, %s, %s)",
            (now, source_name, 1 if ok else 0, int(query_count), error),
        )
        await self._execute(
            "DELETE FROM dns_source_samples WHERE ts < %s", (now - _DNS_RETENTION_SECONDS,)
        )

    async def insert_dns_thrash_observations(self, rows: list[dict[str, Any]]) -> None:
        """Batch of near-threshold standings (ADR-0012), pruned on write like db.py."""
        pool = self._require_pool("insert_dns_thrash_observations")
        if not rows:
            return
        now = time.time()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO dns_thrash_observations "
                "(ts, mac, domain, query_count, threshold, over_since) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
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
        await self._execute(
            "DELETE FROM dns_thrash_observations WHERE ts < %s", (now - _DNS_RETENTION_SECONDS,)
        )

    async def recent_kick_timestamps(self, mac: str, *, since: float) -> list[float]:
        """Logical-kick timestamps for the ADR-0007 caps — semantics as in db.py."""
        pool = self._require_pool("recent_kick_timestamps")
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT ts FROM kick_events WHERE mac = %s AND ts >= %s AND dry_run = 0 "
                "AND mechanism != 'deauth_fallback' ORDER BY ts",
                (mac, since),
            )
            rows = await cur.fetchall()
        return [float(row[0]) for row in rows]

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
        self.closed = True
