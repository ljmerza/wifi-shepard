from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .controllers.base import Controller
from .db import Store
from .notify import Notifier
from .pipeline import DetectionPipeline, build_pipeline

if TYPE_CHECKING:
    from .actor import Actor
    from .backoff import BackoffManager
    from .rate_limit import KickRateLimiter
    from .scorer import Scorer


class Scanner:
    """Per-controller poll loop: fetch wireless clients, persist a sample each,
    and hand each sample to the detection pipeline.

    The pipeline (scorer / backoff / rate-limiter / actor) is injected.
    Production builds it at the composition root (``main.Daemon`` via
    ``build_pipeline``) and passes ``pipeline=``. The ``config=`` / ``ha=``
    arguments are a convenience for tests and ad-hoc embedding: they build a
    pipeline in place. Passing neither yields scan-only mode (samples are
    persisted, nothing is scored or kicked).
    """

    def __init__(
        self,
        *,
        controller: Controller,
        db: Store,
        poll_interval_seconds: float = 60.0,
        config: Any | None = None,
        ha: Notifier | None = None,
        pipeline: DetectionPipeline | None = None,
    ) -> None:
        self.controller = controller
        self.db = db
        self.poll_interval_seconds = poll_interval_seconds
        self.config = config
        if pipeline is None and config is not None:
            pipeline = build_pipeline(config, controller=controller, db=db, ha=ha)
        self._pipeline = pipeline

    # Read accessors for the live collaborators. Callers (tests, embedders) reach
    # for scanner.actor / .backoff / .scorer / .rate_limiter; the pipeline holds
    # them. None in scan-only mode (no pipeline).
    @property
    def scorer(self) -> Scorer | None:
        return self._pipeline.scorer if self._pipeline is not None else None

    @property
    def backoff(self) -> BackoffManager | None:
        return self._pipeline.backoff if self._pipeline is not None else None

    @property
    def rate_limiter(self) -> KickRateLimiter | None:
        return self._pipeline.rate_limiter if self._pipeline is not None else None

    @property
    def actor(self) -> Actor | None:
        return self._pipeline.actor if self._pipeline is not None else None

    def update_config(self, config: Any) -> None:
        self.config = config
        self.poll_interval_seconds = config.scanner.poll_interval_seconds
        if self._pipeline is not None:
            self._pipeline.update_config(config)

    async def run_once(self) -> None:
        clients = await self.controller.list_wireless_clients()
        for client in clients:
            await self.db.insert_sample(client)
            pipeline = self._pipeline
            if pipeline is None:
                continue
            # Emit kick_succeeded / kick_no_roam if this MAC was kicked last cycle.
            pipeline.actor.check_post_kick_outcome(client)
            decision = pipeline.scorer.ingest(client)
            if decision is not None:
                await pipeline.actor.handle(client, decision)
            # ADR-0010: independent traffic-inactivity path. Feeds the same actor, so
            # dry-run, backoff, caps, rate-limits, and HA notification all apply
            # unchanged; the decision dict carries trigger=inactivity for the log line.
            inactivity_decision = pipeline.inactivity.ingest(client)
            if inactivity_decision is not None:
                await pipeline.actor.handle(client, inactivity_decision)

        # Persist AP-level health (identity + CPU/mem + per-radio CU) for the
        # read-only UI's "noisy APs" view. Display-only — never feeds detection.
        for ap in await self.controller.list_ap_stats():
            await self.db.insert_ap_stats(ap)
