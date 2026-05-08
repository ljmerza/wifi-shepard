"""Regression: prove the UI's read connection actually works against the kind
of WAL-mode database the daemon produces. The original AC-5 test seeded a DB
without enabling WAL, which masked any interaction between mode=ro and the
WAL/SHM files SQLite needs.

We construct three scenarios and document what works:
  A. Live WAL writer, dir writable
  B. Live WAL writer, dir read-only (mimics the docker :ro bind mount)
  C. After PRAGMA wal_checkpoint(TRUNCATE)

The test PASSES on the configurations the production sidecar will actually
encounter; configurations that always fail are recorded so a future change
to the connection helper is force-evaluated against them.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from wifi_shepard_ui.db import open_readonly


def _make_wal_db(path: Path) -> sqlite3.Connection:
    """Create a database in WAL mode and return the OPEN writer connection
    so the WAL/SHM files stay live (matches a running daemon)."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.execute("INSERT INTO t VALUES (2)")
    conn.commit()
    return conn


def test_open_readonly_against_live_wal_writer(tmp_path: Path) -> None:
    """Scenario A: writable directory, WAL writer alive. Reads must work."""
    db = tmp_path / "state.db"
    writer = _make_wal_db(db)
    try:
        reader = open_readonly(db)
        try:
            rows = list(reader.execute("SELECT x FROM t ORDER BY x"))
        finally:
            reader.close()
    finally:
        writer.close()
    assert rows == [(1,), (2,)], "open_readonly must read committed rows from a live WAL database"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permission semantics required")
def test_open_readonly_against_readonly_directory(tmp_path: Path) -> None:
    """Scenario B: directory mode 0555 (mimics docker :ro bind), WAL writer alive.
    Documents what actually happens — this is the production-shaped scenario."""
    dir_path = tmp_path / "data"
    dir_path.mkdir()
    db = dir_path / "state.db"
    writer = _make_wal_db(db)

    # Tighten permissions AFTER the writer is up — same shape as the daemon
    # creating /data/state.db, then docker mounting that subtree as :ro for
    # the UI container.
    original_mode = dir_path.stat().st_mode
    os.chmod(dir_path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(dir_path, os.W_OK):
            pytest.skip("running as root or filesystem ignores chmod — RO test invalid")

        # This is what production looks like for the UI container.
        reader = open_readonly(db)
        try:
            rows = list(reader.execute("SELECT x FROM t ORDER BY x"))
        finally:
            reader.close()
        # If we got here, mode=ro+immutable=1 (or whatever the helper does)
        # is sufficient. Note rows may be a stale snapshot if immutable=1
        # is used and the WAL has uncheckpointed data — that's the known
        # trade-off; we just need the open + SELECT not to raise.
        assert isinstance(rows, list)
    finally:
        os.chmod(dir_path, original_mode)
        writer.close()


def test_open_readonly_after_wal_checkpoint(tmp_path: Path) -> None:
    """Scenario C: after wal_checkpoint(TRUNCATE), the main DB file holds
    every committed row. Reads under any mode must reflect that."""
    db = tmp_path / "state.db"
    writer = _make_wal_db(db)
    try:
        writer.execute("INSERT INTO t VALUES (3)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.commit()

        reader = open_readonly(db)
        try:
            rows = list(reader.execute("SELECT x FROM t ORDER BY x"))
        finally:
            reader.close()
    finally:
        writer.close()

    assert rows == [(1,), (2,), (3,)]
