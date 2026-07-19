from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from .main import build_daemon


def run() -> None:
    logging.basicConfig(
        level=os.environ.get("WIFI_SHEPARD_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config_path = Path(os.environ.get("WIFI_SHEPARD_CONFIG", "/config/config.yaml"))
    db_path = Path(os.environ.get("WIFI_SHEPARD_DB", "/data/state.db"))
    # Optional MySQL/MariaDB backend: a single URL env var replaces the SQLite
    # file entirely (WIFI_SHEPARD_DB is ignored while it is set). Unset/empty
    # keeps the default SQLite behavior. A malformed URL raises a clear
    # ValueError here — fail closed, never half-run.
    db_url = os.environ.get("WIFI_SHEPARD_DB_URL") or None
    daemon = build_daemon(config_path=config_path, db_path=db_path, db_url=db_url)
    sys.exit(asyncio.run(daemon.run()))


if __name__ == "__main__":
    run()
