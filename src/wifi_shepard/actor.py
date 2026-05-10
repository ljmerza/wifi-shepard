from __future__ import annotations

import logging
import uuid
from typing import Any

from .scorer import resolve_kick_mechanism

logger = logging.getLogger("wifi_shepard.actor")


def _dispatch_mechanism(resolved: str) -> str:
    """Map a resolved kick_mechanism to the wire-level mechanism the actor sends.

    auto-mode sends BTM speculatively first (ADR-0003 §Decision); the deauth
    fallback path is handled by the actor's _pending_btm state machine.
    Unknown values raise — config validation must reject them at parse time."""
    if resolved in ("btm", "auto"):
        return "btm"
    if resolved == "deauth":
        return "deauth"
    raise RuntimeError(
        f"actor: unknown kick_mechanism {resolved!r} (config validation should "
        "have rejected this at parse time — open a bug)"
    )


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
        # Tracks the ap_id at the moment of each kick so the next scan cycle's
        # post-kick check can emit kick_succeeded / kick_no_roam (ADR-0003 AC-6).
        # Keyed by MAC; values: {"ap_id": str, "mechanism": str, "attempt_group": str}.
        self._pending_outcome: dict[str, dict[str, str]] = {}

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
            would_send = _dispatch_mechanism(resolved)
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
            self._record_pending_outcome(
                mac=mac,
                ap_id=client.ap_id,
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
        sent_mechanism = _dispatch_mechanism(mechanism)
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
        self._record_pending_outcome(
            mac=mac,
            ap_id=client.ap_id,
            mechanism=sent_mechanism,
            attempt_group=attempt_group,
        )
        if self.ha is not None:
            await self.ha.notify(mac, severity="kick")

    def _record_pending_outcome(
        self, *, mac: str, ap_id: str, mechanism: str, attempt_group: str
    ) -> None:
        self._pending_outcome[mac] = {
            "ap_id": ap_id,
            "mechanism": mechanism,
            "attempt_group": attempt_group,
        }

    def check_post_kick_outcome(self, client: Any) -> None:
        """Emit kick_succeeded / kick_no_roam for any kick this MAC took on the
        previous cycle (ADR-0003 AC-6). Called once per polled client by Scanner."""
        pending = self._pending_outcome.pop(client.mac, None)
        if pending is None:
            return
        from_ap = pending["ap_id"]
        to_ap = client.ap_id
        message = "kick_succeeded" if from_ap != to_ap else "kick_no_roam"
        logger.info(
            message,
            extra={
                "mac": client.mac,
                "from_ap": from_ap,
                "to_ap": to_ap,
                "mechanism": pending["mechanism"],
                "attempt_group": pending["attempt_group"],
            },
        )
