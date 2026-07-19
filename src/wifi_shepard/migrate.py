"""One-shot SQLite → MySQL/MariaDB data migration for wifi-shepard.

Copies every daemon table from the SQLite file into the database named by a
``WIFI_SHEPARD_DB_URL``-style URL, so an instance switching backends keeps its
history (device timelines, the kick ledger the ADR-0007 rate caps read, AP/DNS
observability). Row ids are copied verbatim — the UI's latest-per-MAC queries
key on ``MAX(id)``, and MySQL/MariaDB's AUTO_INCREMENT automatically continues
past the highest copied id.

The target schema is created by the daemon's own MySQL backend
(``MySQLDatabase.connect()``), so a migration and a fresh daemon boot can never
disagree on DDL. Columns are copied by name-intersection, so a SQLite file from
an older daemon (missing e.g. ``client_samples.name``) migrates cleanly.

Usage — stop the daemon first so the source file is quiescent, then:

    python -m wifi_shepard.migrate --sqlite /data/state.db \\
        --db-url "$WIFI_SHEPARD_DB_URL"

``--db-url`` defaults to the WIFI_SHEPARD_DB_URL env var; ``--sqlite`` defaults
to /data/state.db (the container path — point it at the volume's state.db when
running from a checkout). Fail-closed: the copy refuses to run into a non-empty
target table; pass ``--replace`` to TRUNCATE the target tables first, which is
what makes a re-run (or a botched first attempt) safe to repeat.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

import aiomysql

from .db_mysql import MySQLDatabase, parse_db_url

logger = logging.getLogger("wifi_shepard.migrate")

# Every table the daemon owns, in db.py schema order. No foreign keys anywhere,
# so copy order doesn't matter beyond readability.
TABLES = (
    "client_samples",
    "kick_events",
    "reboot_events",
    "ap_samples",
    "ap_radio_samples",
    "dns_source_samples",
    "dns_thrash_observations",
)

_BATCH_SIZE = 1000


def _select_sql(table: str, columns: list[str]) -> str:
    """SQLite-side SELECT, ordered by id so batches stream deterministically.

    Identifiers are double-quoted (standard SQL) because kick_events.trigger is
    a SQLite keyword too.
    """
    cols = ", ".join(f'"{c}"' for c in columns)
    return f'SELECT {cols} FROM "{table}" ORDER BY "id"'


def _insert_sql(table: str, columns: list[str]) -> str:
    """MySQL-side INSERT; backticked identifiers cover the reserved column
    names (`signal`, `trigger`) the daemon's schema carries."""
    cols = ", ".join(f"`{c}`" for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    return f"INSERT INTO `{table}` ({cols}) VALUES ({placeholders})"


def _source_columns(conn: sqlite3.Connection, table: str) -> list[str] | None:
    """Column names in the SQLite table, or None when the table doesn't exist
    (a state.db from an older daemon predates the DNS/AP tables)."""
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    if not rows:
        return None
    return [row[1] for row in rows]


async def _target_columns(cur: aiomysql.Cursor, table: str) -> list[str]:
    await cur.execute(f"SHOW COLUMNS FROM `{table}`")
    return [row[0] for row in await cur.fetchall()]


async def _migrate(*, sqlite_path: Path, db_url: str, replace: bool, batch_size: int) -> int:
    # Let the daemon's own backend create/upgrade the target schema first.
    target_db = MySQLDatabase(db_url)
    await target_db.connect()
    await target_db.close()

    target = parse_db_url(db_url)
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn = await aiomysql.connect(
        host=target.host,
        port=target.port,
        user=target.user,
        password=target.password,
        db=target.database,
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        cur = await conn.cursor()

        # Fail-closed guard: never interleave migrated rows into live data.
        occupied: list[str] = []
        for table in TABLES:
            await cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            (count,) = await cur.fetchone()
            if count:
                occupied.append(f"{table} ({count} rows)")
        if occupied and not replace:
            logger.error(
                "target tables are not empty: %s — re-run with --replace to "
                "TRUNCATE them first, or point --db-url at an empty database",
                ", ".join(occupied),
            )
            return 1
        if occupied:
            for table in TABLES:
                await cur.execute(f"TRUNCATE TABLE `{table}`")
            await conn.commit()
            logger.info("truncated %d target tables (--replace)", len(TABLES))

        total = 0
        for table in TABLES:
            source_cols = _source_columns(src, table)
            if source_cols is None:
                logger.info("%s: not present in source, skipped", table)
                continue
            target_cols = set(await _target_columns(cur, table))
            columns = [c for c in source_cols if c in target_cols]
            insert = _insert_sql(table, columns)
            src_cur = src.execute(_select_sql(table, columns))
            copied = 0
            while True:
                rows = src_cur.fetchmany(batch_size)
                if not rows:
                    break
                await cur.executemany(insert, rows)
                copied += len(rows)
            await conn.commit()

            (source_count,) = src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            await cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            (target_count,) = await cur.fetchone()
            if target_count != source_count:
                logger.error(
                    "%s: copied %d but target holds %d of %d source rows",
                    table,
                    copied,
                    target_count,
                    source_count,
                )
                return 1
            logger.info("%s: %d rows migrated", table, copied)
            total += copied

        logger.info("done: %d rows across %d tables -> %s", total, len(TABLES), target.database)
        return 0
    finally:
        conn.close()
        src.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m wifi_shepard.migrate",
        description="Copy wifi-shepard state from the SQLite file to MySQL/MariaDB.",
    )
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=Path("/data/state.db"),
        help="source SQLite file (default: /data/state.db)",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("WIFI_SHEPARD_DB_URL"),
        help="target mysql://user:password@host:3306/database URL (default: $WIFI_SHEPARD_DB_URL)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="TRUNCATE non-empty target tables before copying (makes re-runs safe)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_BATCH_SIZE,
        help=f"rows per INSERT batch (default: {_BATCH_SIZE})",
    )
    args = parser.parse_args(argv)
    if not args.db_url:
        parser.error("--db-url is required (or set WIFI_SHEPARD_DB_URL)")
    if not args.sqlite.exists():
        logger.error("source SQLite file not found: %s", args.sqlite)
        return 1
    return asyncio.run(
        _migrate(
            sqlite_path=args.sqlite,
            db_url=args.db_url,
            replace=args.replace,
            batch_size=args.batch_size,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
