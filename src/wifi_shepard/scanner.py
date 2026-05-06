from __future__ import annotations

import asyncio
from typing import Any


class Scanner:
    def __init__(
        self,
        *,
        controller: Any,
        db: Any,
        poll_interval_seconds: float = 60.0,
    ) -> None:
        self.controller = controller
        self.db = db
        self.poll_interval_seconds = poll_interval_seconds

    async def run_once(self) -> None:
        clients = await self.controller.list_wireless_clients()
        for client in clients:
            await self.db.insert_sample(client)

    async def run(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.poll_interval_seconds)
