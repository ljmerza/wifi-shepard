from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any

from .config import Config, load_config_from_path
from .db import Database
from .scanner import Scanner

logger = logging.getLogger("wifi_shepard.main")


class Daemon:
    def __init__(
        self,
        *,
        config_path: Path,
        db_path: Path,
        controllers: list[Any],
        ha: Any | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.db_path = Path(db_path)
        self.controllers = list(controllers)
        self.ha = ha
        self.config: Config = load_config_from_path(self.config_path)
        self.db = Database(self.db_path)
        self._scanners: list[Scanner] = []
        self.first_cycle_started = asyncio.Event()
        self.config_reloaded = asyncio.Event()
        self._shutdown = asyncio.Event()
        self.exit_code = 0

    def _build_scanners(self) -> list[Scanner]:
        return [
            Scanner(
                controller=c,
                db=self.db,
                poll_interval_seconds=self.config.scanner.poll_interval_seconds,
                config=self.config,
                ha=self.ha,
            )
            for c in self.controllers
        ]

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
        loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        try:
            await self.db.connect()
            self._scanners = self._build_scanners()
            first = True
            while not self._shutdown.is_set():
                for scanner in self._scanners:
                    await scanner.run_once()
                if first:
                    self.first_cycle_started.set()
                    first = False
                interval = self.config.scanner.poll_interval_seconds
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                except TimeoutError:
                    pass
        finally:
            for sig in (signal.SIGHUP, signal.SIGTERM):
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass

    def _on_sighup(self) -> None:
        try:
            new_config = load_config_from_path(self.config_path)
        except Exception as exc:
            logger.error(
                "config_reload_failed",
                extra={"error": str(exc), "path": str(self.config_path)},
            )
            return
        self.config = new_config
        self.config_reloaded.set()

    def _on_sigterm(self) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        self._shutdown.set()


def build_daemon(
    *,
    config_path: Path,
    db_path: Path,
    controllers: list[Any],
    ha: Any | None = None,
) -> Daemon:
    return Daemon(
        config_path=config_path,
        db_path=db_path,
        controllers=controllers,
        ha=ha,
    )
