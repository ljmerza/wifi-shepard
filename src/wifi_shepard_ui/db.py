"""Read-only DB connection helpers for the sidecar.

The UI must never write to the daemon's database. For the default SQLite file
we enforce that twice:
1. The compose fragment mounts the volume :ro (kernel-level guarantee).
2. This module's connections open the URI with `mode=ro` and set
   `query_only=ON` (SQLite-level guarantee). Either fence catches the
   other failing — defense in depth.

When WIFI_SHEPARD_DB_URL selects the MySQL/MariaDB backend instead,
`MySQLReadConnection` plays the same role: it wraps a pymysql connection in
the small sqlite3-shaped surface views.py uses (`execute(sql, params)` with
`?` placeholders, tuple rows, `sqlite3.OperationalError` on missing tables)
and sets the session's transaction access mode to READ ONLY — the MySQL
analogue of `query_only=ON`.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

# views.py column names that are reserved words in MySQL/MariaDB. The daemon's
# MySQL DDL backticks them; translate_sql() does the same for the UI's queries
# so views.py stays engine-agnostic.
_RESERVED_COLUMNS = ("signal", "trigger")

# pymysql error codes translated into the sqlite3.OperationalError vocabulary
# views.py / app.py already handle for the partial-deploy windows.
_ER_BAD_FIELD = 1054  # unknown column → "no such column"
_ER_NO_SUCH_TABLE = 1146  # missing table → "no such table"
# Connection-level failures where the server simply isn't reachable — mapped to
# the "unable to open database" marker so a down MariaDB renders the same
# empty-state page a missing SQLite file does (auth errors are NOT mapped; a
# wrong password should surface loudly, not as an empty page).
_CONNECT_ERRORS = (2002, 2003, 2013)


def open_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Return a sqlite3 Connection that rejects every DML/DDL statement.

    Uses the SQLite URI form `file:...?mode=ro` and additionally sets
    `PRAGMA query_only=ON`. Either fence on its own would catch INSERT/
    UPDATE/DELETE/CREATE/DROP attempts; combined they survive an
    unintended fallback to read-write file mode (e.g. if a future
    deployment forgot the docker `:ro` bind on the volume).

    Note for the WAL case: when the daemon writes in WAL mode, mode=ro
    readers still need to read the existing -wal/-shm files. POSIX
    shared-lock acquisition on those files does not require directory
    write permission, so this works on `/data` mounted `:ro` (verified
    in tests/ui/test_wal_compat.py).
    """
    db_path = Path(db_path)
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def translate_sql(sql: str) -> str:
    """Rewrite one of views.py's SQLite statements into MySQL/MariaDB dialect.

    - `?` placeholders → `%s` (pymysql paramstyle). No query in views.py
      carries a literal `?` inside a string constant, so a plain substitution
      is safe.
    - ` COLLATE NOCASE` dropped: the daemon creates the tables under a
      utf8mb4 case-insensitive collation, so plain equality already matches
      MACs case-insensitively.
    - reserved column names (`signal`, `trigger`) backticked, matching the
      daemon's MySQL DDL.
    """
    out = sql.replace(" COLLATE NOCASE", "").replace("?", "%s")
    for col in _RESERVED_COLUMNS:
        out = re.sub(rf"(?<![`\w]){col}(?![`\w])", f"`{col}`", out)
    return out


class MySQLReadConnection:
    """sqlite3-shaped read-only view over a pymysql connection.

    Exposes exactly the surface views.py/app.py use — `execute(sql, params)`
    returning an iterable cursor with fetchone()/fetchall(), plus close() —
    and re-raises missing-table/column errors as sqlite3.OperationalError with
    the same message markers ("no such table", "no such column") so the
    empty-state and partial-deploy tolerance in the callers works unchanged.
    """

    def __init__(self, db_url: str) -> None:
        import pymysql

        # Reuse the daemon's fail-closed URL parser (single source of truth for
        # the WIFI_SHEPARD_DB_URL shape) — same precedent as config_io reusing
        # the daemon's config validation.
        from wifi_shepard.db_mysql import parse_db_url

        self._pymysql = pymysql
        target = parse_db_url(db_url)
        try:
            self._conn = pymysql.connect(
                host=target.host,
                port=target.port,
                user=target.user,
                password=target.password,
                database=target.database,
                charset="utf8mb4",
                autocommit=True,
            )
        except pymysql.MySQLError as e:
            code = e.args[0] if e.args else None
            if code in _CONNECT_ERRORS:
                raise sqlite3.OperationalError(f"unable to open database: {e}") from e
            raise
        # Soft read-only fence, mirroring open_readonly()'s query_only=ON: with
        # autocommit every statement is its own transaction, and READ ONLY
        # access mode makes the server reject any DML/DDL in it.
        with self._conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        cur = self._conn.cursor()
        try:
            cur.execute(translate_sql(sql), params)
        except self._pymysql.MySQLError as e:
            cur.close()
            code = e.args[0] if e.args else None
            if code == _ER_NO_SUCH_TABLE:
                raise sqlite3.OperationalError(f"no such table: {e.args[1]}") from e
            if code == _ER_BAD_FIELD:
                raise sqlite3.OperationalError(f"no such column: {e.args[1]}") from e
            raise
        return cur

    def table_columns(self, table: str) -> set[str] | None:
        """Column names of `table`, or None when the table doesn't exist.

        The MySQL stand-in for the sqlite_master + PRAGMA table_info pair in
        views.assert_kick_events_schema.
        """
        try:
            cur = self.execute(f"SHOW COLUMNS FROM {table}")
        except sqlite3.OperationalError:
            return None
        with cur:
            return {row[0] for row in cur.fetchall()}

    def close(self) -> None:
        self._conn.close()


def open_readonly_any(
    db_path: Path | str, db_url: str | None = None
) -> sqlite3.Connection | MySQLReadConnection:
    """Open the read-side backend matching the daemon's: URL set → MySQL, else SQLite."""
    if db_url:
        return MySQLReadConnection(db_url)
    return open_readonly(db_path)
