from __future__ import annotations

import asyncio
from typing import Any

from .actor import Actor
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
        self.scorer: Scorer | None = Scorer(config) if config is not None else None
        self.actor: Actor | None = Actor(config, ha) if config is not None else None

    async def run_once(self) -> None:
        clients = await self.controller.list_wireless_clients()
        for client in clients:
            await self.db.insert_sample(client)
            if self.scorer is None or self.actor is None:
                continue
            decision = self.scorer.ingest(client)
            if decision is not None:
                await self.actor.handle(client, decision)

    async def run(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.poll_interval_seconds)
