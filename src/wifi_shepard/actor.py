from __future__ import annotations

import logging
import uuid
from typing import Any

from .scorer import resolve_kick_mechanism

logger = logging.getLogger("wifi_shepard.actor")


class Actor:
    def __init__(
        self,
        *,
        config: Any,
        controller: Any,
        db: Any,
        ha: Any | None = None,
        backoff: Any | None = None,
    ) -> None:
        self.config = config
        self.controller = controller
        self.db = db
        self.ha = ha
        self.backoff = backoff

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

        if self.backoff is not None and self.backoff.should_quarantine(mac):
            if self.ha is not None and not self.backoff.quarantine_notified(mac):
                await self.ha.notify(mac, severity="quarantine")
                self.backoff.mark_quarantine_notified(mac)
            return

        if self.backoff is not None:
            self.backoff.record_kick(mac)
        mechanism = resolve_kick_mechanism(mac, self.config)
        attempt_group = str(uuid.uuid4())
        if mechanism == "btm":
            await self.controller.send_btm_request(mac, target_bssid=None)
        else:
            await self.controller.force_reconnect_client(mac)
        await self.db.insert_kick(
            mac=mac,
            dry_run=False,
            mechanism=mechanism,
            attempt_group=attempt_group,
        )
        if self.ha is not None:
            await self.ha.notify(mac, severity="kick")
