"""DNS-thrash detector (ADR-0011).

A wedged application-layer client (the 2026-07-15 Meross incident) re-resolves the
same broker hostname on every failed connect — 478 of its last 500 queries for one
name — while its WiFi telemetry stays pristine, so the scorer never sees it. This
detector counts per-(MAC, domain) query timestamps in a trailing window and flags a
MAC whose over-threshold condition has held continuously for a sustain duration.

It is an *additive* signal: a flagged MAC is routed through the exact same
``Actor.handle`` path as a scorer flag (dry-run, backoff, caps, rate limits, and HA
notification all apply unchanged). A source fetch failure is logged and yields no
flags for that cycle — the DNS signal must never break the scan loop.

Time is injected (``now_fn``) the way ``Scorer`` takes ``wall_now_fn`` so tests drive
the clock; production uses wall-clock ``time.time`` to match the sources' unix
timestamps.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .dns_sources.base import DnsSource
from .resolution import resolve_dns_same_domain_max

logger = logging.getLogger("wifi_shepard.dns_thrash")


class DnsThrashDetector:
    def __init__(
        self,
        config: Any,
        source: DnsSource,
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.source = source
        # Wall clock (unix) — DnsQuery.ts and the sustain window are wall-clock
        # concepts, and the fetch `from` bound must match the resolver's clock.
        self.now_fn = now_fn
        # (mac -> (domain -> deque[query ts])). Timestamps accumulate across polls and
        # are pruned to the trailing window on each evaluation.
        self._counts: dict[str, dict[str, deque[float]]] = {}
        # mac -> wall time the over-threshold condition began (reset when it clears).
        self._over_since: dict[str, float] = {}
        # Fetch only the delta since the last poll so a query is counted once; None on
        # the first poll bootstraps with one window of history.
        self._last_poll_ts: float | None = None
        # ADR-0012: per-(MAC, domain) standings from the most recent observe() —
        # count/threshold/over_since — so the scanner can persist near-threshold
        # contenders for the UI. Recomputed each poll from live window state.
        self._last_standings: list[dict[str, Any]] = []

    def standings(self) -> list[dict[str, Any]]:
        """Per-(MAC, domain) standings from the last observe() (ADR-0012)."""
        return list(self._last_standings)

    def update_config(self, config: Any) -> None:
        # SIGHUP retune: swap the config so the next poll resolves the new thresholds.
        # The deques are time-pruned (no count-bound maxlen), so no rebuild is needed.
        self.config = config

    async def observe(self, clients: list[Any]) -> list[str]:
        cfg = self.config.detection.dns_thrash
        now = self.now_fn()
        window_seconds = cfg.window_minutes * 60
        sustain_seconds = cfg.sustain_windows * cfg.window_minutes * 60

        # MAC<->IP join comes from the controller's client list (ADR-0011 AC-8);
        # clients with no known IP are skipped (fail-soft ClientSnapshot.ip).
        ip_to_mac: dict[str, str] = {}
        for client in clients:
            ip = getattr(client, "ip", None)
            if isinstance(ip, str) and ip:
                ip_to_mac[ip] = client.mac

        since = self._last_poll_ts if self._last_poll_ts is not None else now - window_seconds
        try:
            queries = await self.source.queries_since(since)
        except Exception:
            # The additive signal must never break the scan loop (ADR-0011 AC-10).
            logger.warning("dns_source_unavailable")
            self._last_poll_ts = now
            return []
        self._last_poll_ts = now

        for query in queries:
            mac = ip_to_mac.get(query.client_ip)
            if mac is None:
                # Query from an IP with no matching client (or hardcoded-DNS blind
                # spot) — ignored.
                continue
            self._counts.setdefault(mac, {}).setdefault(query.domain, deque()).append(query.ts)

        cutoff = now - window_seconds
        flagged: list[str] = []
        standings: list[dict[str, Any]] = []
        for mac in list(self._counts.keys()):
            domains = self._counts[mac]
            threshold = resolve_dns_same_domain_max(mac, self.config)
            over = False
            live_domains: list[tuple[str, int]] = []
            for domain in list(domains.keys()):
                timestamps = domains[domain]
                while timestamps and timestamps[0] < cutoff:
                    timestamps.popleft()
                if not timestamps:
                    del domains[domain]
                    continue
                count = len(timestamps)
                live_domains.append((domain, count))
                if count > threshold:
                    over = True
            if not domains:
                # No live history left — drop the MAC (and its streak marker) so
                # disconnected/quiet devices don't accumulate forever.
                del self._counts[mac]
                self._over_since.pop(mac, None)
                continue
            if not over:
                self._over_since.pop(mac, None)
            else:
                started = self._over_since.get(mac)
                if started is None:
                    started = now
                    self._over_since[mac] = now
                if now - started >= sustain_seconds:
                    flagged.append(mac)
            # ADR-0012: record standings for every live (MAC, domain) — including
            # not-yet-over contenders — with the MAC's finalized streak marker.
            over_since = self._over_since.get(mac)
            for domain, count in live_domains:
                standings.append(
                    {
                        "mac": mac,
                        "domain": domain,
                        "count": count,
                        "threshold": threshold,
                        "over_since": over_since,
                    }
                )
        self._last_standings = standings
        return flagged
