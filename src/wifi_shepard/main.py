from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import datetime
from pathlib import Path

from .config import Config, load_config_from_path
from .controllers import Controller, build_controller
from .db import Database
from .notify import Notifier
from .pipeline import build_pipeline
from .reboot.ha_resolver import HADeviceRegistry
from .reboot.rebooter import Rebooter
from .reboot.scheduler import RebootScheduler
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
        rebooter: Rebooter | None = None,
        registry: HADeviceRegistry | None = None,
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
        self.rebooter = rebooter
        self.registry = registry
        self.db = Database(self.db_path)
        self._scanners: list[Scanner] = []
        self._scheduler: RebootScheduler | None = self._build_scheduler()
        self.first_cycle_started = asyncio.Event()
        self.config_reloaded = asyncio.Event()
        self.config_reload_attempted = asyncio.Event()
        self._shutdown = asyncio.Event()
        self.exit_code = 0

    def _build_scheduler(self) -> RebootScheduler | None:
        # ADR-0006 AC-10: no scheduler task unless reboot + proactive are both on.
        rc = self.config.reboot
        if not (rc.enabled and rc.proactive.enabled):
            return None
        # The concrete HA reboot backend (rebooter) + device registry are injected.
        # Until that client lands (deferred follow-up), proactive config can be set
        # but has nothing to fire through — skip the scheduler and say so.
        if self.rebooter is None or self.registry is None:
            logger.warning(
                "reboot_proactive_no_backend",
                extra={"detail": "proactive reboot enabled but no rebooter/registry wired"},
            )
            return None
        return RebootScheduler(
            config=self.config,
            registry=self.registry,
            rebooter=self.rebooter,
            db=self.db,
            ha=self.ha,
        )

    async def _run_scheduler(self) -> None:
        # Tick at most every 30s so an HH:MM schedule is never skipped, regardless
        # of the (possibly longer) scan poll interval.
        assert self._scheduler is not None
        tick = min(self.config.scanner.poll_interval_seconds, 30)
        while not self._shutdown.is_set():
            try:
                await self._scheduler.run_due(datetime.now())
            except Exception:
                # Never let a tick failure kill the long-lived scheduler task —
                # it would only surface when awaited on shutdown. Log and tick on.
                logger.exception("reboot_scheduler_tick_failed")
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=tick)
            except TimeoutError:
                pass

    def _build_scanners(self) -> list[Scanner]:
        # Composition root: build each controller's pipeline here and inject it,
        # so Scanner receives ready collaborators instead of wiring them itself.
        return [
            Scanner(
                controller=c,
                db=self.db,
                poll_interval_seconds=self.config.scanner.poll_interval_seconds,
                config=self.config,
                pipeline=build_pipeline(self.config, controller=c, db=self.db, ha=self.ha),
            )
            for c in self.controllers
        ]

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
        loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        scheduler_task: asyncio.Task[None] | None = None
        try:
            await self.db.connect()
            for controller in self.controllers:
                await controller.login()
            self._scanners = self._build_scanners()
            if self._scheduler is not None:
                scheduler_task = asyncio.create_task(self._run_scheduler())
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
            if scheduler_task is not None:
                scheduler_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await scheduler_task
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
        # ADR-0006 AC-12: retune the proactive schedule + cooldown in place without
        # purging in-memory last-reboot state. (A reload that flips proactive on/off
        # mid-run does not start/stop the task here — out of Phase 1 scope.)
        if self._scheduler is not None:
            self._scheduler.update_config(new_config)
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
    rebooter: Rebooter | None = None,
    registry: HADeviceRegistry | None = None,
) -> Daemon:
    return Daemon(
        config_path=config_path,
        db_path=db_path,
        controllers=controllers,
        ha=ha,
        rebooter=rebooter,
        registry=registry,
    )
