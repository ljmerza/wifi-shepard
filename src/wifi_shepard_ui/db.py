"""SQLite connection helper for the read-only sidecar.

The UI must never write to /data/state.db. We enforce that twice:
1. The compose fragment mounts the volume :ro (kernel-level guarantee).
2. This module's connections open the URI with `mode=ro` and set
   `query_only=ON` (SQLite-level guarantee). Either fence catches the
   other failing — defense in depth.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


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
