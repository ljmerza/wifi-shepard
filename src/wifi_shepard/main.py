from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from .config import Config, load_config_from_path
from .controllers import Controller, build_controller
from .db import Database
from .notify import Notifier
from .scanner import Scanner

logger = logging.getLogger("wifi_shepard.main")


class Daemon:
    def __init__(
        self,
        *,
        config_path: Path,
        db_path: Path,
        controllers: list[Controller] | None = None,
        ha: Notifier | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.db_path = Path(db_path)
        self.config: Config = load_config_from_path(self.config_path)
        if controllers is None:
            if not self.config.controllers:
                raise ValueError(
                    "no controllers configured: set controllers: in config.yaml "
                    "or inject a list via build_daemon(controllers=...)"
                )
            controllers = [build_controller(spec) for spec in self.config.controllers]
        self.controllers = list(controllers)
        self.ha = ha
        self.db = Database(self.db_path)
        self._scanners: list[Scanner] = []
        self.first_cycle_started = asyncio.Event()
        self.config_reloaded = asyncio.Event()
        self.config_reload_attempted = asyncio.Event()
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

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
        loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        try:
            await self.db.connect()
            for controller in self.controllers:
                await controller.login()
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
            return self.exit_code
        finally:
            for sig in (signal.SIGHUP, signal.SIGTERM):
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass
            await self.db.close()
            for controller in self.controllers:
                try:
                    await controller.close()
                except Exception:
                    logger.exception("controller_close_failed")
            if self.ha is not None:
                try:
                    await self.ha.close()
                except Exception:
                    logger.exception("ha_close_failed")

    def _on_sighup(self) -> None:
        try:
            new_config = load_config_from_path(self.config_path)
        except Exception:
            logger.exception(
                "config_reload_failed",
                extra={"path": str(self.config_path)},
            )
            return
        finally:
            self.config_reload_attempted.set()
        self.config = new_config
        for scanner in self._scanners:
            scanner.update_config(new_config)
        self.config_reloaded.set()

    def _on_sigterm(self) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        self._shutdown.set()


def build_daemon(
    *,
    config_path: Path,
    db_path: Path,
    controllers: list[Controller] | None = None,
    ha: Notifier | None = None,
) -> Daemon:
    return Daemon(
        config_path=config_path,
        db_path=db_path,
        controllers=controllers,
        ha=ha,
    )
