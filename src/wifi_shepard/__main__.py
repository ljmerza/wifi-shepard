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
    daemon = build_daemon(config_path=config_path, db_path=db_path)
    sys.exit(asyncio.run(daemon.run()))


if __name__ == "__main__":
    run()
