"""Notification sink abstraction.

Mirrors the ``Controller`` Protocol (controllers/base.py): the daemon depends on
this surface, and concrete channels (Home Assistant REST, future MQTT, ...)
implement it without the scorer/actor knowing which channel they talk to. A
new channel is a new class, not an edit to the actor.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["Notifier"]


@runtime_checkable
class Notifier(Protocol):
    async def notify(self, mac: str, *, severity: str) -> None:
        """Emit a notification for ``mac`` at the given severity (e.g. "kick",
        "quarantine"). Implementations may accept extra optional arguments."""
        ...

    async def close(self) -> None:
        """Release any held resources (HTTP session, sockets). Called once on
        daemon shutdown; part of the lifecycle contract, not duck-typed."""
        ...
