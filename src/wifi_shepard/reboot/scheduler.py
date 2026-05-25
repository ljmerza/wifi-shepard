"""Proactive reboot scheduler (ADR-0006 Phase 1).

Reboots opt-in MACs at a daily HH:MM local time, reusing ADR-0005's resolver to
turn an eligible MAC into a concrete HA reboot target. The scheduler separates
"is it due?" (clock matching) from "fire" (per-MAC reboot) so the per-MAC path
is unit-testable without a real wall clock.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING

from wifi_shepard.reboot.cooldown import RebootCooldown
from wifi_shepard.reboot.eligibility import is_reboot_eligible
from wifi_shepard.reboot.ha_resolver import resolve_reboot_target

if TYPE_CHECKING:
    from wifi_shepard.config import Config
    from wifi_shepard.db import Store
    from wifi_shepard.notify import Notifier
    from wifi_shepard.reboot.ha_resolver import HADeviceRegistry
    from wifi_shepard.reboot.rebooter import Rebooter

logger = logging.getLogger("wifi_shepard.reboot")


class RebootScheduler:
    def __init__(
        self,
        *,
        config: Config,
        registry: HADeviceRegistry,
        rebooter: Rebooter,
        db: Store,
        ha: Notifier | None = None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.registry = registry
        self.rebooter = rebooter
        self.db = db
        self.ha = ha
        self.now_fn = now_fn
        self.cooldown = RebootCooldown(
            per_device_seconds=config.reboot.cooldown.per_device_seconds,
            max_per_device_per_day=config.reboot.cooldown.max_per_device_per_day,
        )
        self._last_fired_date: date | None = None

    def is_due(self, now: datetime) -> bool:
        proactive = self.config.reboot.proactive
        if not proactive.enabled:
            return False
        if now.strftime("%H:%M") != proactive.schedule:
            return False
        # Fire at most once per calendar day even if the loop ticks several times
        # within the scheduled minute.
        return now.date() != self._last_fired_date

    async def run_due(self, now: datetime) -> None:
        if not self.is_due(now):
            return
        self._last_fired_date = now.date()
        for mac in self.config.reboot.eligible:
            await self.attempt(mac, mode="proactive")

    async def attempt(self, mac: str, *, mode: str = "proactive") -> None:
        # Allowlist + opt-in are absolute (ADR-0006 AC-5): an ineligible MAC is
        # dropped before any path — no reboot, no dry-run preview, no audit row.
        if not is_reboot_eligible(mac, self.config):
            return
        if self.config.reboot.dry_run:
            # Preview only: log would_reboot and write an audit row flagged
            # dry_run=1 (symmetry with the fired path, ADR-0004 AC-6), make no
            # network call. Mirrors the would_kick bypass.
            target = await resolve_reboot_target(mac, self.config, self.registry)
            entity = target.entity_id if target is not None else None
            logger.info("would_reboot", extra={"mac": mac, "mode": mode, "target": entity})
            await self.db.insert_reboot(
                mac=mac, mode=mode, outcome="dry_run", target=entity, dry_run=True
            )
            return
        now = self.now_fn()
        allowed, reason, retry = self.cooldown.can_reboot(mac, now=now)
        if not allowed:
            logger.info(
                "reboot_deferred",
                extra={"mac": mac, "mode": mode, "reason": reason, "retry_after_seconds": retry},
            )
            return
        target = await resolve_reboot_target(mac, self.config, self.registry)
        if target is None:
            return  # resolver already logged reboot_target_unresolved
        await self.rebooter.reboot(target)
        self.cooldown.record_reboot(mac, now=self.now_fn())
        await self.db.insert_reboot(
            mac=mac, mode=mode, outcome="fired", target=target.entity_id, dry_run=False
        )
        logger.info("reboot_fired", extra={"mac": mac, "mode": mode, "target": target.entity_id})
        if self.ha is not None:
            await self.ha.notify(mac, severity="reboot")
