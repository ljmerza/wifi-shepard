"""Per-device reboot cooldown (ADR-0006), mirroring the ADR-0004 KickRateLimiter.

No notion of wall-clock time: callers pass `now` (typically time.monotonic), so
tests advance the clock without monkey-patching `time`. State is in-memory and
lost on restart, bounded by the daily cap.
"""

from __future__ import annotations


class RebootCooldown:
    def __init__(self, *, per_device_seconds: int, max_per_device_per_day: int) -> None:
        self.per_device_seconds = per_device_seconds
        self.max_per_device_per_day = max_per_device_per_day
        self._last_reboot_at: dict[str, float] = {}

    def can_reboot(self, mac: str, *, now: float) -> tuple[bool, str | None, float | None]:
        """Returns (allowed, reason, retry_after_seconds). reason is 'cooldown'
        when the per-device single-flight window has not yet elapsed."""
        last = self._last_reboot_at.get(mac)
        if self.per_device_seconds > 0 and last is not None:
            elapsed = now - last
            if elapsed < self.per_device_seconds:
                return False, "cooldown", self.per_device_seconds - elapsed
        return True, None, None

    def record_reboot(self, mac: str, *, now: float) -> None:
        self._last_reboot_at[mac] = now
