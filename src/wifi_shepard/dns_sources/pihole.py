"""Pi-hole v6 FTL REST DNS source + a multi-instance merging composite (ADR-0011).

``PiholeSource`` reads per-client query history from ONE Pi-hole v6 instance:

    POST {url}/api/auth   {"password": ...}      -> {"session": {"sid": ...}}
    GET  {url}/api/queries?from=<unix>&length=N  -> {"queries": [{time, client:{ip}, domain}]}

The session id (``sid``) is sent on each query request via the ``X-FTL-SID`` header
(Pi-hole v6 also accepts a ``sid`` query param or cookie; the header keeps the secret
out of the request line / any URL logging). An expired sid answers 401 — the source
re-authenticates exactly once and retries the fetch once.

``MergedDnsSource`` fans one logical source out across several instances (a client's
queries can land entirely on one of two resolvers), concatenating their results and
tolerating per-instance failure: a down Pi-hole logs a warning and contributes
nothing, so one dead instance never blinds the other.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .base import DnsQuery, DnsSource

logger = logging.getLogger("wifi_shepard.dns_sources")

_TIMEOUT_SECONDS = 15
# Pi-hole v6 /api/queries defaults to length=100; a busy resolver produces far more
# than that per poll, so request a generous page to avoid silently truncating the
# very thrash we are trying to count.
_DEFAULT_LENGTH = 10000


class _ExpiredSession(Exception):
    """Internal: the query request was rejected 401 (sid expired) — re-auth and retry."""


class PiholeSource:
    """One Pi-hole v6 FTL instance. Owns its aiohttp session lazily (created in
    ``login()``, torn down in ``close()``), like ``UniFiController``."""

    def __init__(
        self,
        *,
        url: str,
        password: str,
        length: int = _DEFAULT_LENGTH,
        name: str = "pihole",
    ) -> None:
        self._url = url.rstrip("/")
        self._password = password
        self._length = length
        self.name = name
        self._session: aiohttp.ClientSession | None = None
        self._sid: str | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        # Lazy: the Daemon constructs sources before the event loop starts, and
        # ClientSession must be created inside a running loop.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            )
        return self._session

    async def login(self) -> None:
        await self._authenticate()

    async def _authenticate(self) -> None:
        session = self._ensure_session()
        async with session.post(f"{self._url}/api/auth", json={"password": self._password}) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"pihole auth failed for {self.name}: HTTP {resp.status}")
            data = await resp.json()
        sid = (data.get("session") or {}).get("sid")
        if not sid or not isinstance(sid, str):
            raise RuntimeError(f"pihole auth response for {self.name} had no session.sid")
        self._sid = sid

    async def queries_since(self, since: float) -> list[DnsQuery]:
        if self._sid is None:
            await self._authenticate()
        try:
            rows = await self._request_queries(since)
        except _ExpiredSession:
            # Expired sid: re-authenticate once and retry the fetch once (ADR-0011).
            await self._authenticate()
            rows = await self._request_queries(since)
        return _parse_rows(rows)

    async def _request_queries(self, since: float) -> list[Any]:
        session = self._ensure_session()
        async with session.get(
            f"{self._url}/api/queries",
            params={"from": int(since), "length": self._length},
            headers={"X-FTL-SID": self._sid or ""},
        ) as resp:
            if resp.status == 401:
                raise _ExpiredSession
            if resp.status >= 400:
                raise RuntimeError(
                    f"pihole queries fetch failed for {self.name}: HTTP {resp.status}"
                )
            data = await resp.json()
        queries = data.get("queries")
        return queries if isinstance(queries, list) else []

    async def close(self) -> None:
        session = self._session
        self._session = None
        self._sid = None
        if session is not None:
            await session.close()


def _parse_rows(rows: list[Any]) -> list[DnsQuery]:
    """Map Pi-hole v6 /api/queries rows to ``DnsQuery``.

    Verified key names against the FTL OpenAPI spec: each row has ``time`` (number),
    ``client`` (nested ``{ip, name}``), and ``domain`` (string). Rows missing a time,
    client ip, or domain are skipped rather than raised on — a single malformed row
    must not sink the whole poll.
    """
    out: list[DnsQuery] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = row.get("time")
        domain = row.get("domain")
        client = row.get("client")
        client_ip = client.get("ip") if isinstance(client, dict) else client
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if not isinstance(domain, str) or not isinstance(client_ip, str):
            continue
        out.append(DnsQuery(ts=float(ts), client_ip=client_ip, domain=domain))
    return out


class MergedDnsSource:
    """Fan a logical source out across several instances, merging their queries and
    tolerating per-instance failure (ADR-0011)."""

    def __init__(self, sources: list[DnsSource]) -> None:
        self._sources = list(sources)
        # ADR-0012: per-instance outcome of the most recent queries_since, so the
        # scanner can persist a per-poll health heartbeat (name/ok/query_count/error)
        # even when nothing is flagged. Empty until the first poll.
        self._last_poll_status: list[dict[str, Any]] = []

    def last_poll_status(self) -> list[dict[str, Any]]:
        """Per-instance outcome of the last ``queries_since`` (ADR-0012)."""
        return list(self._last_poll_status)

    async def login(self) -> None:
        # Tolerate a down instance at startup: one Pi-hole being unreachable must not
        # prevent the other from serving the signal.
        for source in self._sources:
            try:
                await source.login()
            except Exception:
                logger.warning(
                    "dns_source_login_failed", extra={"source": getattr(source, "name", "?")}
                )

    async def queries_since(self, since: float) -> list[DnsQuery]:
        results = await asyncio.gather(
            *(source.queries_since(since) for source in self._sources),
            return_exceptions=True,
        )
        merged: list[DnsQuery] = []
        status: list[dict[str, Any]] = []
        for source, result in zip(self._sources, results, strict=True):
            name = getattr(source, "name", "?")
            if isinstance(result, BaseException):
                logger.warning("dns_source_unavailable", extra={"source": name})
                status.append(
                    {"name": name, "ok": False, "query_count": 0, "error": str(result)}
                )
                continue
            merged.extend(result)
            status.append(
                {"name": name, "ok": True, "query_count": len(result), "error": None}
            )
        self._last_poll_status = status
        return merged

    async def close(self) -> None:
        for source in self._sources:
            try:
                await source.close()
            except Exception:
                logger.warning(
                    "dns_source_close_failed", extra={"source": getattr(source, "name", "?")}
                )
