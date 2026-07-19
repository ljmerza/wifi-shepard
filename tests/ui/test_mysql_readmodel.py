"""WIFI_SHEPARD_DB_URL read-side adapter: SQL translation, error mapping,
schema assert dispatch, and the empty-state behavior when the DB server is
unreachable — all without a live MySQL/MariaDB (the live server is exercised
at deploy time)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pymysql
import pytest
from fastapi.testclient import TestClient

from wifi_shepard_ui import views
from wifi_shepard_ui.app import create_app
from wifi_shepard_ui.db import MySQLReadConnection, open_readonly_any, translate_sql

# A local port with nothing listening: connection refused immediately, which the
# adapter must surface as the "unable to open database" empty-state marker.
_UNREACHABLE_URL = "mysql://u:p@127.0.0.1:9/db"


def test_translate_placeholders() -> None:
    assert (
        translate_sql("SELECT ts FROM kick_events WHERE mac = ? LIMIT ?")
        == "SELECT ts FROM kick_events WHERE mac = %s LIMIT %s"
    )


def test_translate_strips_collate_nocase() -> None:
    out = translate_sql("SELECT ts FROM kick_events WHERE mac = ? COLLATE NOCASE ORDER BY ts")
    assert "COLLATE" not in out
    assert "mac = %s ORDER BY ts" in out


def test_translate_backticks_reserved_columns() -> None:
    assert (
        translate_sql("SELECT ts, signal, radio FROM client_samples")
        == "SELECT ts, `signal`, radio FROM client_samples"
    )
    assert translate_sql("WHERE trigger = 'dns_thrash'") == "WHERE `trigger` = 'dns_thrash'"


def test_translate_respects_word_boundaries() -> None:
    # Column names merely containing a reserved word must not be mangled, and an
    # already-backticked name must not gain a second layer.
    assert translate_sql("SELECT wifi_tx_attempts FROM t") == "SELECT wifi_tx_attempts FROM t"
    assert translate_sql("SELECT `signal` FROM t") == "SELECT `signal` FROM t"


def _adapter_with_error(exc: Exception) -> MySQLReadConnection:
    """Adapter over a fake connection whose cursor raises `exc` on execute()."""

    class _FakeCursor:
        def execute(self, sql: str, params=()) -> None:
            raise exc

        def close(self) -> None:
            pass

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    adapter = MySQLReadConnection.__new__(MySQLReadConnection)
    adapter._pymysql = pymysql
    adapter._conn = _FakeConn()
    return adapter


def test_missing_table_maps_to_no_such_table() -> None:
    exc = pymysql.err.ProgrammingError(1146, "Table 'wifi_shepard.kick_events' doesn't exist")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _adapter_with_error(exc).execute("SELECT ts FROM kick_events")


def test_missing_column_maps_to_no_such_column() -> None:
    exc = pymysql.err.OperationalError(1054, "Unknown column 'name' in 'field list'")
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        _adapter_with_error(exc).execute("SELECT name FROM client_samples")


def test_other_mysql_errors_pass_through() -> None:
    # e.g. access denied must surface as the original driver error (a 500 +
    # log line), never be masked as an empty-state page.
    exc = pymysql.err.OperationalError(1045, "Access denied for user 'u'@'host'")
    with pytest.raises(pymysql.err.OperationalError):
        _adapter_with_error(exc).execute("SELECT 1")


def test_open_readonly_any_defaults_to_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    sqlite3.connect(db_path).close()
    conn = open_readonly_any(db_path)
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()


def test_unreachable_server_maps_to_unable_to_open() -> None:
    with pytest.raises(sqlite3.OperationalError, match="unable to open database"):
        MySQLReadConnection(_UNREACHABLE_URL)


class _StubIntrospection:
    """Just enough of the adapter's surface for assert_kick_events_schema."""

    def __init__(self, columns: set[str] | None) -> None:
        self._columns = columns

    def table_columns(self, table: str) -> set[str] | None:
        return self._columns


def test_schema_assert_tolerates_missing_table_via_adapter() -> None:
    views.assert_kick_events_schema(_StubIntrospection(None))  # must not raise


def test_schema_assert_flags_missing_columns_via_adapter() -> None:
    with pytest.raises(views.SchemaMismatch):
        views.assert_kick_events_schema(_StubIntrospection({"id", "ts", "mac"}))


def test_schema_assert_passes_on_full_schema_via_adapter() -> None:
    views.assert_kick_events_schema(
        _StubIntrospection({"id", "ts", "mac", "dry_run", "mechanism", "attempt_group", "trigger"})
    )


def test_app_renders_empty_state_when_db_server_unreachable(tmp_path: Path) -> None:
    # Mirrors test_ac_8_missing_db: a down MariaDB behaves like a missing
    # SQLite file — empty-state pages, not 500s.
    app = create_app(db_path=tmp_path / "absent.db", db_url=_UNREACHABLE_URL)
    client = TestClient(app)
    for path in ("/", "/devices", "/dns"):
        response = client.get(path)
        assert response.status_code == 200, path
