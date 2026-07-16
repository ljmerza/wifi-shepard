"""DNS data-source abstraction (ADR-0011).

A ``DnsSource`` is an *additive* data source alongside the ``Controller`` — the
UniFi controller can't see per-client DNS, so DNS-thrash detection reads query
history from an external resolver (Pi-hole v6 first). The scanner/detector depend
on this surface; concrete backends (``PiholeSource``) implement it without the
detector knowing which resolver it talks to, exactly mirroring the ``Controller``
and ``Notifier`` Protocols.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class DnsQuery:
    """One resolved DNS query, normalized across backends.

    ``ts`` is a wall-clock unix timestamp (seconds, fractional allowed) so it can be
    compared directly against the detector's injected wall clock. ``client_ip`` is the
    resolver's view of who asked; the detector joins it to a MAC via the controller's
    client list. ``domain`` is the queried name (exact FQDN in v1 — eTLD+1 grouping is
    deferred, see ADR-0011).
    """

    ts: float
    client_ip: str
    domain: str


@runtime_checkable
class DnsSource(Protocol):
    """Brand-agnostic per-client DNS query source.

    Lifecycle mirrors ``Controller``: ``login()`` once at startup (establish any
    session), paired with ``close()`` on shutdown. Backends that need no session
    step may implement ``login()`` as a no-op, but the method must exist — the
    lifecycle is part of the contract, not duck-typed.
    """

    async def login(self) -> None:
        """Establish the source session (authenticate). Called once at startup."""
        ...

    async def queries_since(self, since: float) -> list[DnsQuery]:
        """Return every query observed at or after ``since`` (unix seconds).

        Fail-soft is the caller's concern for the *detection* signal, but a source
        that can partially answer (e.g. a multi-instance composite with one instance
        down) should return what it has rather than raise.
        """
        ...

    async def close(self) -> None:
        """Release any held resources (HTTP session). Called once on shutdown."""
        ...
