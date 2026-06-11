"""Concrete Home Assistant REST notifier.

Implements the ``Notifier`` Protocol (notify/__init__.py) against HA's
``POST /api/services/notify/<service>`` endpoint — the PLAN.md §1/§4 per-kick
and per-quarantine notifications.

Delivery is best-effort by design: a down or misconfigured HA is logged
(``notify_failed``) and never raised — losing a notification must not abort
the kick path or crash the daemon.
"""

from __future__ import annotations

import logging

import aiohttp

from wifi_shepard.config import HomeAssistantConfig

logger = logging.getLogger("wifi_shepard.notify")

_TIMEOUT_SECONDS = 10

# Operator-facing message per severity (the actor emits "kick" / "quarantine").
# An unknown severity still delivers via the fallback — the transport layer
# never drops a notification on the floor.
_MESSAGES: dict[str, str] = {
    "kick": "wifi-shepard kicked {mac} to force a roam (sustained bad 2.4 GHz state).",
    "quarantine": (
        "wifi-shepard quarantined {mac} after repeated ineffective kicks — "
        "this device may be defective."
    ),
}
_FALLBACK_MESSAGE = "wifi-shepard: {mac} ({severity})."


class HomeAssistantNotifier:
    def __init__(self, config: HomeAssistantConfig) -> None:
        self._url = config.url.rstrip("/")
        self._token = config.token
        self._service = config.notify_service
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        # Lazy: the Daemon constructs the notifier before the event loop starts,
        # and ClientSession must be created inside a running loop.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            )
        return self._session

    async def notify(self, mac: str, *, severity: str) -> None:
        template = _MESSAGES.get(severity, _FALLBACK_MESSAGE)
        payload = {
            "title": f"wifi-shepard: {severity}",
            "message": template.format(mac=mac, severity=severity),
        }
        try:
            async with self._ensure_session().post(
                f"{self._url}/api/services/notify/{self._service}",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            ) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "notify_failed",
                        extra={"mac": mac, "severity": severity, "status": resp.status},
                    )
        except (TimeoutError, aiohttp.ClientError, OSError):
            logger.exception("notify_failed", extra={"mac": mac, "severity": severity})

    async def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            await session.close()
