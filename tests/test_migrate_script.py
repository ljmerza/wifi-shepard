"""python -m wifi_shepard.migrate: the pure parts of the SQLite→MySQL copier.

The end-to-end copy needs a live MySQL/MariaDB and is exercised at deploy time;
these tests pin the per-dialect SQL generation (identifier quoting for the
reserved/keyword columns) and the source-side introspection the copy relies on.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from wifi_shepard.db import SCHEMA_KICK_EVENTS
from wifi_shepard.migrate import TABLES, _insert_sql, _select_sql, _source_columns


def test_select_sql_double_quotes_sqlite_identifiers() -> None:
    # `trigger` is a keyword in SQLite too — bare identifiers would be fragile.
    assert (
        _select_sql("kick_events", ["ts", "mac", "trigger"])
        == 'SELECT "ts", "mac", "trigger" FROM "kick_events" ORDER BY "id"'
    )


def test_insert_sql_backticks_mysql_identifiers() -> None:
    assert (
        _insert_sql("client_samples", ["ts", "signal"])
        == "INSERT INTO `client_samples` (`ts`, `signal`) VALUES (%s, %s)"
    )


def test_source_columns_reads_sqlite_schema(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(SCHEMA_KICK_EVENTS)
    conn.commit()
    try:
        columns = _source_columns(conn, "kick_events")
        assert columns is not None
        assert {"id", "ts", "mac", "dry_run", "mechanism", "trigger"} <= set(columns)
    finally:
        conn.close()


def test_source_columns_none_for_missing_table(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "empty.db")
    try:
        assert _source_columns(conn, "dns_thrash_observations") is None
    finally:
        conn.close()


def test_tables_cover_every_daemon_table() -> None:
    # If db.py grows a table, the migration must learn it too.
    assert TABLES == (
        "client_samples",
        "kick_events",
        "reboot_events",
        "ap_samples",
        "ap_radio_samples",
        "dns_source_samples",
        "dns_thrash_observations",
    )
