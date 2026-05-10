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
        # Tracks in-flight BTM attempts so the next scan cycle can fall back to
        # deauth under the same attempt_group when the client did not roam
        # (ADR-0003 AC-4). Keyed by MAC; values: {"group": str, "ap_id": str}.
        self._pending_btm: dict[str, dict[str, str]] = {}

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
            resolved = resolve_kick_mechanism(mac, self.config)
            would_send = "btm" if resolved in ("btm", "auto") else "deauth"
            logger.info(
                "would_kick",
                extra={
                    "mac": mac,
                    "thresholds": thresholds,
                    "reason": reason,
                    "mechanism": would_send,
                },
            )
            return

        if self.backoff is not None and self.backoff.should_quarantine(mac):
            if self.ha is not None and not self.backoff.quarantine_notified(mac):
                await self.ha.notify(mac, severity="quarantine")
                self.backoff.mark_quarantine_notified(mac)
            return

        # If we sent BTM on a previous cycle and the client is still bad-state on
        # the same AP, fall back to deauth under the same attempt_group. The pair
        # counts as ONE logical kick — record_kick already fired on the BTM cycle.
        pending = self._pending_btm.get(mac)
        if pending is not None and pending["ap_id"] == client.ap_id:
            await self.controller.force_reconnect_client(mac)
            await self.db.insert_kick(
                mac=mac,
                dry_run=False,
                mechanism="deauth_fallback",
                attempt_group=pending["group"],
            )
            del self._pending_btm[mac]
            return

        if self.backoff is not None:
            self.backoff.record_kick(mac)
        mechanism = resolve_kick_mechanism(mac, self.config)
        attempt_group = str(uuid.uuid4())
        # auto-mode is speculative BTM-then-deauth-fallback (ADR-0003 §Decision):
        # always send BTM first, then on the next scan cycle if the client did not
        # roam, fall back to deauth under the same attempt_group. Recorded as 'btm'
        # in the row; the fallback path above writes the second 'deauth_fallback' row.
        sent_mechanism = "btm" if mechanism in ("btm", "auto") else "deauth"
        if sent_mechanism == "btm":
            await self.controller.send_btm_request(mac, target_bssid=None)
            self._pending_btm[mac] = {"group": attempt_group, "ap_id": client.ap_id}
        else:
            await self.controller.force_reconnect_client(mac)
        await self.db.insert_kick(
            mac=mac,
            dry_run=False,
            mechanism=sent_mechanism,
            attempt_group=attempt_group,
        )
        if self.ha is not None:
            await self.ha.notify(mac, severity="kick")
