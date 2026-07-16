from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from .controllers.base import Controller
from .db import Store
from .notify import Notifier
from .pipeline import DetectionPipeline, build_pipeline
from .reboot import normalize_mac

if TYPE_CHECKING:
    from .actor import Actor
    from .backoff import BackoffManager
    from .dns_thrash import DnsThrashDetector
    from .rate_limit import KickRateLimiter
    from .scorer import Scorer

logger = logging.getLogger("wifi_shepard.scanner")

# ADR-0012: cap how many near-threshold contenders are persisted per poll so a
# noisy cycle can't write an unbounded batch of observation rows.
_DNS_OBSERVATION_TOP_N = 20


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
        dns_detector: DnsThrashDetector | None = None,
    ) -> None:
        self.controller = controller
        self.db = db
        self.poll_interval_seconds = poll_interval_seconds
        self.config = config
        if pipeline is None and config is not None:
            pipeline = build_pipeline(config, controller=controller, db=db, ha=ha)
        self._pipeline = pipeline
        # ADR-0011: optional DNS-thrash detector (built at the composition root when
        # the feature is configured). None = feature off; the scan loop is unchanged.
        self._dns_detector = dns_detector

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
        # ADR-0011: SIGHUP retune of the DNS-thrash thresholds in place (source
        # URL/password changes still require a restart — see ADR-0011).
        if self._dns_detector is not None:
            self._dns_detector.update_config(config)

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

        # ADR-0011: DNS-thrash detection is an additive signal. A flagged MAC routes
        # through the *same* Actor.handle as a scorer flag, so dry-run, backoff, caps,
        # rate limits, and HA notification all apply unchanged.
        await self._run_dns_thrash(clients)

        # Persist AP-level health (identity + CPU/mem + per-radio CU) for the
        # read-only UI's "noisy APs" view. Display-only — never feeds detection.
        for ap in await self.controller.list_ap_stats():
            await self.db.insert_ap_stats(ap)

    async def _run_dns_thrash(self, clients: list[Any]) -> None:
        detector = self._dns_detector
        pipeline = self._pipeline
        if detector is None or pipeline is None:
            return
        flagged = await detector.observe(clients)
        # ADR-0012: persist observability every cycle — even when nothing is flagged,
        # so the UI can prove the source authenticated and polled. Fail-soft: a write
        # error here must never break the scan loop or the RF remediation path below.
        try:
            await self._persist_dns_observability(detector)
        except Exception:
            logger.warning("dns_observability_persist_failed")
        if not flagged:
            return
        client_by_mac = {client.mac: client for client in clients}
        allowlist = self.config.allowlist if self.config is not None else ()
        for mac in flagged:
            # The allowlist is the primary safety control; a flagged-but-allowlisted
            # MAC is never kicked (compared canonically, like the scorer does).
            if normalize_mac(mac) in allowlist:
                continue
            client = client_by_mac.get(mac)
            if client is None:
                # Flagged from accumulated history but not present this cycle
                # (disconnected) — nothing to kick.
                continue
            await pipeline.actor.handle(client, {"trigger": "dns_thrash"})

    async def _persist_dns_observability(self, detector: Any) -> None:
        # ADR-0012: write a per-poll health heartbeat (one row per DNS instance).
        source = getattr(detector, "source", None)
        if source is not None and hasattr(source, "last_poll_status"):
            for st in source.last_poll_status():
                await self.db.insert_dns_source_sample(
                    source_name=st["name"],
                    ok=st["ok"],
                    query_count=st["query_count"],
                    error=st.get("error"),
                )
        # Persist only contenders (count >= half the threshold), top-N capped, so
        # quiet domains don't flood the table.
        standings = detector.standings() if hasattr(detector, "standings") else []
        contenders = [
            s for s in standings if s["count"] >= math.ceil(0.5 * s["threshold"])
        ]
        contenders.sort(key=lambda s: s["count"], reverse=True)
        if contenders:
            await self.db.insert_dns_thrash_observations(contenders[:_DNS_OBSERVATION_TOP_N])
