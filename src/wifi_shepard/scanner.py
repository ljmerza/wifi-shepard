from __future__ import annotations

from typing import Any

from .actor import Actor
from .backoff import BackoffManager
from .controllers.base import Controller
from .rate_limit import KickRateLimiter
from .scorer import Scorer


class Scanner:
    def __init__(
        self,
        *,
        controller: Controller,
        db: Any,
        poll_interval_seconds: float = 60.0,
        config: Any | None = None,
        ha: Any | None = None,
    ) -> None:
        self.controller = controller
        self.db = db
        self.poll_interval_seconds = poll_interval_seconds
        self.config = config
        self.ha = ha
        if config is not None:
            self.scorer: Scorer | None = Scorer(config)
            self.backoff: BackoffManager | None = BackoffManager(
                quarantine_after_kicks=config.backoff.quarantine_after_kicks,
            )
            self.rate_limiter: KickRateLimiter | None = KickRateLimiter(
                min_seconds_between_kicks=config.safety_rails.min_seconds_between_kicks,
                max_kicks_per_ap_per_window=config.safety_rails.max_kicks_per_ap_per_window,
                per_ap_window_seconds=config.safety_rails.per_ap_window_seconds,
            )
            self.actor: Actor | None = Actor(
                config=config,
                controller=controller,
                db=db,
                ha=ha,
                backoff=self.backoff,
                rate_limiter=self.rate_limiter,
            )
        else:
            self.scorer = None
            self.backoff = None
            self.rate_limiter = None
            self.actor = None

    def update_config(self, config: Any) -> None:
        old_window = self.config.scanner.window_samples if self.config is not None else None
        self.config = config
        self.poll_interval_seconds = config.scanner.poll_interval_seconds
        if self.scorer is not None:
            if old_window != config.scanner.window_samples:
                self.scorer = Scorer(config)
            else:
                self.scorer.config = config
        if self.actor is not None:
            self.actor.config = config
        if self.backoff is not None:
            self.backoff.quarantine_after_kicks = config.backoff.quarantine_after_kicks
        # ADR-0004 AC-8: update rate-limit thresholds in place WITHOUT resetting
        # in-flight state (_last_kick_at, _per_ap_kicks). Operators are tuning,
        # not requesting a state purge.
        if self.rate_limiter is not None:
            self.rate_limiter.min_seconds_between_kicks = (
                config.safety_rails.min_seconds_between_kicks
            )
            self.rate_limiter.max_kicks_per_ap_per_window = (
                config.safety_rails.max_kicks_per_ap_per_window
            )
            self.rate_limiter.per_ap_window_seconds = config.safety_rails.per_ap_window_seconds

    async def run_once(self) -> None:
        clients = await self.controller.list_wireless_clients()
        for client in clients:
            await self.db.insert_sample(client)
            if self.actor is not None:
                # Emit kick_succeeded / kick_no_roam if this MAC was kicked last cycle.
                self.actor.check_post_kick_outcome(client)
            if self.scorer is None or self.actor is None:
                continue
            decision = self.scorer.ingest(client)
            if decision is not None:
                await self.actor.handle(client, decision)
