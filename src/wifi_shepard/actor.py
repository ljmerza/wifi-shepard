from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from .backoff import evaluate_backoff
from .controllers.base import Controller
from .db import Store
from .notify import Notifier
from .pending import PendingKicks
from .rate_limit import KickRateLimiter
from .resolution import resolve_caps, resolve_kick_mechanism

logger = logging.getLogger("wifi_shepard.actor")


def _dispatch_mechanism(resolved: str) -> str:
    """Map a resolved kick_mechanism to the wire-level mechanism the actor sends.

    auto-mode sends BTM speculatively first (ADR-0003 §Decision); the deauth
    fallback path is handled by the actor's pending-BTM state machine (see
    pending.PendingKicks). Unknown values raise — config validation must reject
    them at parse time."""
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
        controller: Controller,
        db: Store,
        ha: Notifier | None = None,
        backoff: Any | None = None,
        rate_limiter: KickRateLimiter | None = None,
        now_fn: Callable[[], float] = time.monotonic,
        wall_now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.controller = controller
        self.db = db
        self.ha = ha
        self.backoff = backoff
        self.rate_limiter = rate_limiter
        # Injected for tests (ADR-0004 Fork J): simulate clock advancement
        # without monkey-patching the `time` module. Production uses the default.
        self.now_fn = now_fn
        # Wall clock for the ADR-0007 per-MAC cooldown/caps, which compare against
        # kick_events.ts (wall-clock time.time()). Distinct from now_fn (monotonic,
        # for the ADR-0004 rate limiter) — the two measure different things.
        self.wall_now_fn = wall_now_fn
        # In-flight kick bookkeeping: the BTM->deauth fallback map (ADR-0003 AC-4)
        # and the post-kick roam-check map (ADR-0003 AC-6). See pending.py.
        self.pending = PendingKicks()

    async def handle(self, client: Any, thresholds: dict[str, Any]) -> None:
        mac = client.mac
        reason = {
            "signal": client.signal,
            "tx_rate_kbps": client.tx_rate_kbps,
            "tx_retries": client.tx_retries,
            "wifi_tx_attempts": client.wifi_tx_attempts,
            "radio": client.radio,
        }
        # Resolve once at the top — used by every code path below. Two call
        # sites would have to keep the ('btm','auto')→'btm' dispatch in
        # lockstep, which is a foot-gun the compiler can't catch.
        resolved_mechanism = resolve_kick_mechanism(mac, self.config)
        sent_mechanism = _dispatch_mechanism(resolved_mechanism)
        # ADR-0012: attribute the kick to the signal that raised it. The scorer's
        # decision dict carries no 'trigger', so an RF flag defaults to 'rf';
        # inactivity/dns paths tag their dict explicitly.
        trigger = thresholds.get("trigger", "rf")

        if self.config.scanner.dry_run:
            logger.info(
                "would_kick",
                extra={
                    "mac": mac,
                    "thresholds": thresholds,
                    "reason": reason,
                    "mechanism": sent_mechanism,
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
        # ClientSnapshot.ap_id is non-Optional per the Protocol contract
        # (controllers/base.py); UniFiController._require raises if ap_mac is
        # missing, so a None here can't reach this code in production.
        pending = self.pending.get_btm(mac)
        if pending is not None and pending["ap_id"] == client.ap_id:
            # ADR-0004 Fork G: the fallback wire call is gated by the global
            # single-flight only, not the per-AP cap (it's the same logical kick).
            if self.rate_limiter is not None:
                now = self.now_fn()
                # Distinct name from the outer `reason` dict (line 63) so the
                # rate-limiter's string reason code can't collide with it.
                allowed, defer_reason, retry = self.rate_limiter.can_wire_call(now=now)
                if not allowed:
                    logger.info(
                        "kick_deferred",
                        extra={
                            "mac": mac,
                            "ap_id": client.ap_id,
                            "reason": defer_reason,
                            "retry_after_seconds": retry,
                            "stage": "deauth_fallback",
                            "attempt_group": pending["group"],
                        },
                    )
                    # Leave the pending BTM in place so the next scan cycle retries.
                    return
            await self.controller.force_reconnect_client(mac)
            if self.rate_limiter is not None:
                self.rate_limiter.record_wire_call(now=self.now_fn())
            await self.db.insert_kick(
                mac=mac,
                dry_run=False,
                mechanism="deauth_fallback",
                attempt_group=pending["group"],
                trigger=trigger,
            )
            self.pending.set_outcome(
                mac,
                ap_id=client.ap_id,
                mechanism="deauth_fallback",
                attempt_group=pending["group"],
            )
            self.pending.clear_btm(mac)
            return

        # Per-MAC backoff: escalating cooldown + hourly/daily caps (ADR-0007),
        # DB-derived from kick_events so the caps survive restart/SIGHUP. Applies to
        # FRESH kicks only — the deauth_fallback above is the same logical kick.
        # Skipped entirely (no DB read) when the feature is off for this MAC.
        cooldowns = tuple(self.config.backoff.cooldowns_seconds)
        max_hour, max_day = resolve_caps(mac, self.config)
        if cooldowns or max_hour > 0 or max_day > 0:
            wall_now = self.wall_now_fn()
            lookback = max(86400.0, float(max(cooldowns)) if cooldowns else 0.0)
            recent = await self.db.recent_kick_timestamps(mac, since=wall_now - lookback)
            allowed, defer_reason, retry = evaluate_backoff(
                recent,
                wall_now,
                cooldowns=cooldowns,
                max_per_hour=max_hour,
                max_per_day=max_day,
            )
            if not allowed:
                logger.info(
                    "kick_deferred",
                    extra={
                        "mac": mac,
                        "ap_id": client.ap_id,
                        "reason": defer_reason,
                        "retry_after_seconds": retry,
                        "stage": "per_mac_backoff",
                    },
                )
                return

        # Fresh kick gate: global single-flight + per-AP cap (ADR-0004).
        if self.rate_limiter is not None:
            now = self.now_fn()
            # Distinct name from the outer `reason` dict (line 63) so the
            # rate-limiter's string reason code can't collide with it.
            allowed, defer_reason, retry = self.rate_limiter.can_kick(client.ap_id, now=now)
            if not allowed:
                logger.info(
                    "kick_deferred",
                    extra={
                        "mac": mac,
                        "ap_id": client.ap_id,
                        "reason": defer_reason,
                        "retry_after_seconds": retry,
                        "stage": "fresh",
                    },
                )
                return

        attempt_group = str(uuid.uuid4())
        # auto-mode is speculative BTM-then-deauth-fallback (ADR-0003 §Decision):
        # always send BTM first, then on the next scan cycle if the client did not
        # roam, fall back to deauth under the same attempt_group. Recorded as 'btm'
        # in the row; the fallback path above writes the second 'deauth_fallback' row.
        # Self-review BLOCKER #2: the controller call is the only step that can
        # raise on a real network. If it raises, NOTHING below this point should
        # execute — no record_kick (would burn budget), no DB row (would record a
        # kick that didn't happen), no notify (would lie to the operator).
        if sent_mechanism == "btm":
            await self.controller.send_btm_request(mac, target_bssid=None)
            self.pending.set_btm(mac, group=attempt_group, ap_id=client.ap_id)
        else:
            await self.controller.force_reconnect_client(mac)
        if self.rate_limiter is not None:
            self.rate_limiter.record_kick(client.ap_id, now=self.now_fn())
        if self.backoff is not None:
            self.backoff.record_kick(mac)
        await self.db.insert_kick(
            mac=mac,
            dry_run=False,
            mechanism=sent_mechanism,
            attempt_group=attempt_group,
            trigger=trigger,
        )
        self.pending.set_outcome(
            mac,
            ap_id=client.ap_id,
            mechanism=sent_mechanism,
            attempt_group=attempt_group,
        )
        if self.ha is not None:
            await self.ha.notify(mac, severity="kick")

    def check_post_kick_outcome(self, client: Any) -> None:
        """Emit kick_succeeded / kick_no_roam for any kick this MAC took on the
        previous cycle (ADR-0003 AC-6). Called once per polled client by Scanner."""
        pending = self.pending.pop_outcome(client.mac)
        if pending is None:
            return
        from_ap = pending["ap_id"]
        to_ap = client.ap_id
        roamed = from_ap != to_ap
        message = "kick_succeeded" if roamed else "kick_no_roam"
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
        # If a BTM kick succeeded (client roamed off the original AP), clear the
        # stale pending-BTM entry. Otherwise it lingers indefinitely and a future
        # bad-state at the original ap_id would fire deauth_fallback under an
        # unrelated attempt_group, corrupting the audit trail and bypassing the
        # backoff budget. The no-roam case is left alone — handle() consumes it
        # on this same cycle via the deauth_fallback path.
        if roamed and pending["mechanism"] == "btm":
            self.pending.clear_btm(client.mac)
