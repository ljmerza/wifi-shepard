from __future__ import annotations

from typing import Any

from .actor import Actor
from .backoff import BackoffManager
from .scorer import Scorer


class Scanner:
    def __init__(
        self,
        *,
        controller: Any,
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
            self.actor: Actor | None = Actor(
                config=config,
                controller=controller,
                db=db,
                ha=ha,
                backoff=self.backoff,
            )
        else:
            self.scorer = None
            self.backoff = None
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

    async def run_once(self) -> None:
        clients = await self.controller.list_wireless_clients()
        for client in clients:
            await self.db.insert_sample(client)
            if self.scorer is None or self.actor is None:
                continue
            decision = self.scorer.ingest(client)
            if decision is not None:
                await self.actor.handle(client, decision)
