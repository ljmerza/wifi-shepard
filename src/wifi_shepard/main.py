from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import signal
from datetime import datetime
from pathlib import Path

from .config import Config, load_config_from_path
from .controllers import Controller, build_controller
from .db import create_database
from .dns_sources import DnsSource, build_dns_sources
from .dns_thrash import DnsThrashDetector
from .notify import HomeAssistantNotifier, Notifier
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
        db_url: str | None = None,
        controllers: list[Controller] | None = None,
        ha: Notifier | None = None,
        rebooter: Rebooter | None = None,
        registry: HADeviceRegistry | None = None,
        config_watch_interval_seconds: float = 5.0,
    ) -> None:
        self.config_path = Path(config_path)
        self.db_path = Path(db_path)
        self.db_url = db_url
        self.config: Config = load_config_from_path(self.config_path)
        if controllers is None:
            if not self.config.controllers:
                raise ValueError(
                    "no controllers configured: set controllers: in config.yaml "
                    "or inject a list via build_daemon(controllers=...)"
                )
            controllers = [build_controller(spec) for spec in self.config.controllers]
        self.controllers = list(controllers)
        if ha is None and self.config.home_assistant is not None:
            # Built once at startup from the home_assistant: block; an injected
            # kwarg (tests, alternate channels) wins. SIGHUP does not rebuild it
            # — same Phase-1 limitation as the reboot-scheduler toggle below.
            ha = HomeAssistantNotifier(self.config.home_assistant)
        self.ha = ha
        self.rebooter = rebooter
        self.registry = registry
        # WIFI_SHEPARD_DB_URL set → MySQL/MariaDB backend; unset → SQLite file.
        self.db = create_database(db_path=self.db_path, db_url=db_url)
        # ADR-0011: optional DNS data source, built once at startup from dns_sources:.
        # None when unconfigured; source URL/password changes require a restart (a
        # SIGHUP retunes only the dns_thrash thresholds, not the source wiring).
        self._dns_source: DnsSource | None = build_dns_sources(self.config)
        self._scanners: list[Scanner] = []
        self._scheduler: RebootScheduler | None = self._build_scheduler()
        self.first_cycle_started = asyncio.Event()
        self.config_reloaded = asyncio.Event()
        self.config_reload_attempted = asyncio.Event()
        self._shutdown = asyncio.Event()
        self.exit_code = 0
        # ADR-0013 Phase 0: file-watch reload. SIGHUP is unreliable in the
        # container (uv is PID 1 and doesn't forward it; a single-file bind
        # mount pins the old inode across an atomic rewrite), so a content-hash
        # poll is the primary reload trigger. Seed the seen-digest from the file
        # we just loaded so the first change — not the initial state — reloads.
        self._config_watch_interval = config_watch_interval_seconds
        self._config_digest_seen = self._config_digest()

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
                dns_detector=self._build_dns_detector(),
            )
            for c in self.controllers
        ]

    def _build_dns_detector(self) -> DnsThrashDetector | None:
        # ADR-0011: one detector per scanner, sharing the merged source. Built only
        # when the feature is configured (config validation guarantees dns_thrash
        # implies a source). Per-scanner state stays isolated to that controller's
        # clients, so multi-controller setups don't cross-count.
        if self.config.detection.dns_thrash is None or self._dns_source is None:
            return None
        return DnsThrashDetector(self.config, self._dns_source)

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, self._on_sighup)
        loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        scheduler_task: asyncio.Task[None] | None = None
        watch_task: asyncio.Task[None] | None = None
        try:
            await self.db.connect()
            for controller in self.controllers:
                await controller.login()
            if self._dns_source is not None:
                # Authenticate the DNS source(s). MergedDnsSource tolerates a down
                # instance at startup (logs + degrades), so this never fails the daemon.
                await self._dns_source.login()
            self._scanners = self._build_scanners()
            if self._scheduler is not None:
                scheduler_task = asyncio.create_task(self._run_scheduler())
            # ADR-0013 Phase 0: start the config file-watch only after scanners
            # exist, so a reload firing on its first tick has them to retune.
            watch_task = asyncio.create_task(self._watch_config())
            first = True
            while not self._shutdown.is_set():
                for scanner in self._scanners:
                    try:
                        await scanner.run_once()
                    except Exception:
                        # A transient controller/network/DB failure must not kill
                        # the long-lived daemon — log and resume next cycle,
                        # mirroring the scheduler-tick guard in _run_scheduler.
                        logger.exception(
                            "scan_cycle_failed",
                            extra={"controller": scanner.controller.name},
                        )
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
            if watch_task is not None:
                watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch_task
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
            if self._dns_source is not None:
                try:
                    await self._dns_source.close()
                except Exception:
                    logger.exception("dns_source_close_failed")
            if self.ha is not None:
                try:
                    await self.ha.close()
                except Exception:
                    logger.exception("ha_close_failed")

    def _config_digest(self) -> str | None:
        """SHA-256 of the config file's bytes, or None if it can't be read.

        None (file transiently absent during an atomic rename, or unreadable) is
        treated as 'no change' so the watch loop skips the tick rather than
        reloading a phantom edit. A rename is atomic, so a successful read always
        returns a whole file (old or new), never a truncated one.
        """
        try:
            return hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        except OSError:
            return None

    def _reload_config(self) -> bool:
        """Re-read config from disk and apply it in place; return True on success.

        Shared by the SIGHUP handler and the file-watch task. A parse/validation
        failure logs config_reload_failed and keeps the last-good config — the
        daemon never half-applies a broken edit (fail-closed, PLAN.md §5).
        """
        try:
            new_config = load_config_from_path(self.config_path)
        except Exception:
            logger.exception(
                "config_reload_failed",
                extra={"path": str(self.config_path)},
            )
            return False
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
        return True

    async def _watch_config(self) -> None:
        """Poll the config file for content changes and hot-reload on change.

        ADR-0013 Phase 0: the primary reload mechanism. In the container PID 1 is
        `uv run`, which does not forward SIGHUP to the daemon child, and a
        single-file bind mount pins the old inode across an atomic rewrite — so
        SIGHUP alone never sees an edit. Mount the config *directory* (not the
        file) so a rewrite's new inode is visible here. A transient read error
        (file mid-rename) is skipped, never fatal; a parse failure keeps the
        last-good config via _reload_config.
        """
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self._config_watch_interval)
            except TimeoutError:
                pass
            if self._shutdown.is_set():
                break
            digest = self._config_digest()
            if digest is not None and digest != self._config_digest_seen:
                # Record the seen digest before reloading so a config that fails
                # to parse isn't retried every tick — the next *edit* (new
                # digest) is what re-arms the reload.
                self._config_digest_seen = digest
                self._reload_config()

    def _on_sighup(self) -> None:
        self._reload_config()

    def _on_sigterm(self) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        self._shutdown.set()


def build_daemon(
    *,
    config_path: Path,
    db_path: Path,
    db_url: str | None = None,
    controllers: list[Controller] | None = None,
    ha: Notifier | None = None,
    rebooter: Rebooter | None = None,
    registry: HADeviceRegistry | None = None,
    config_watch_interval_seconds: float = 5.0,
) -> Daemon:
    return Daemon(
        config_path=config_path,
        db_path=db_path,
        db_url=db_url,
        controllers=controllers,
        ha=ha,
        rebooter=rebooter,
        registry=registry,
        config_watch_interval_seconds=config_watch_interval_seconds,
    )
