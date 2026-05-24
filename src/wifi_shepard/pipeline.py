"""Detection pipeline: construction + config-reload for one controller's
bad-state detection and action components.

Splitting this out of Scanner (which used to ``new`` all four collaborators in
its constructor and hand-sync them on reload) lets Scanner be just a poll loop.
The composition root (``main.Daemon``) calls ``build_pipeline`` and injects the
result; Scanner receives ready collaborators rather than knowing how to wire
them (SOLID review H2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .actor import Actor
from .backoff import BackoffManager
from .controllers.base import Controller
from .db import Store
from .notify import Notifier
from .rate_limit import KickRateLimiter
from .scorer import Scorer


@dataclass
class DetectionPipeline:
    """The scorer / backoff / rate-limiter / actor for one controller.

    Holds the live collaborators and owns the SIGHUP config-reload, applied in
    place so accumulated per-MAC state survives the reload.
    """

    scorer: Scorer
    backoff: BackoffManager
    rate_limiter: KickRateLimiter
    actor: Actor

    def update_config(self, config: Any) -> None:
        # Rebuild the scorer only when the window size changes (its deque maxlen
        # is fixed at construction); otherwise swap the config reference so the
        # accumulated per-MAC sample windows survive the reload.
        if self.scorer.config.scanner.window_samples != config.scanner.window_samples:
            self.scorer = Scorer(config)
        else:
            self.scorer.config = config
        self.actor.config = config
        self.backoff.quarantine_after_kicks = config.backoff.quarantine_after_kicks
        # ADR-0004 AC-8: update rate-limit thresholds in place WITHOUT resetting
        # in-flight state (_last_kick_at, _per_ap_kicks). Operators are tuning,
        # not requesting a state purge.
        self.rate_limiter.min_seconds_between_kicks = config.safety_rails.min_seconds_between_kicks
        self.rate_limiter.max_kicks_per_ap_per_window = (
            config.safety_rails.max_kicks_per_ap_per_window
        )
        self.rate_limiter.per_ap_window_seconds = config.safety_rails.per_ap_window_seconds


def build_pipeline(
    config: Any,
    *,
    controller: Controller,
    db: Store,
    ha: Notifier | None = None,
) -> DetectionPipeline:
    """Wire a controller's detection pipeline from config. The single place that
    knows the collaborators' construction order and dependencies."""
    backoff = BackoffManager(quarantine_after_kicks=config.backoff.quarantine_after_kicks)
    rate_limiter = KickRateLimiter(
        min_seconds_between_kicks=config.safety_rails.min_seconds_between_kicks,
        max_kicks_per_ap_per_window=config.safety_rails.max_kicks_per_ap_per_window,
        per_ap_window_seconds=config.safety_rails.per_ap_window_seconds,
    )
    actor = Actor(
        config=config,
        controller=controller,
        db=db,
        ha=ha,
        backoff=backoff,
        rate_limiter=rate_limiter,
    )
    scorer = Scorer(config)
    return DetectionPipeline(scorer=scorer, backoff=backoff, rate_limiter=rate_limiter, actor=actor)
