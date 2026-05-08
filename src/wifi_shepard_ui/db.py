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

    Uses the SQLite URI form `file:...?mode=ro&immutable=0`. `mode=ro`
    forbids writes at the SQLite VFS level; the additional `query_only`
    PRAGMA blocks any INSERT/UPDATE/DELETE/CREATE/DROP that might slip
    through a read-write file mode (defensive — `mode=ro` already does it).
    """
    db_path = Path(db_path)
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn
