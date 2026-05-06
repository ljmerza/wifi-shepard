from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("wifi_shepard.actor")


class Actor:
    def __init__(self, config: Any, ha: Any | None = None) -> None:
        self.config = config
        self.ha = ha

    async def handle(self, client: Any, thresholds: dict[str, Any]) -> None:
        mac = client.mac
        reason = {
            "signal": client.signal,
            "tx_rate_kbps": client.tx_rate_kbps,
            "tx_retries": client.tx_retries,
            "wifi_tx_attempts": client.wifi_tx_attempts,
            "radio": client.radio,
        }
        if self.config.scanner.dry_run:
            logger.info(
                "would_kick",
                extra={"mac": mac, "thresholds": thresholds, "reason": reason},
            )
            return
