"""Per-device reboot cooldown (ADR-0006), mirroring the ADR-0004 KickRateLimiter.

No notion of wall-clock time: callers pass `now` (typically time.monotonic), so
tests advance the clock without monkey-patching `time`. State is in-memory and
lost on restart, bounded by the daily cap.
"""

from __future__ import annotations

from collections import deque

# "per day" is a rolling 24h window (ADR-0006 design decision): deterministic
# under the injected monotonic clock and reuses ADR-0004's deque/prune pattern.
_DAY_SECONDS = 86400


class RebootCooldown:
    def __init__(self, *, per_device_seconds: int, max_per_device_per_day: int) -> None:
        self.per_device_seconds = per_device_seconds
        self.max_per_device_per_day = max_per_device_per_day
        self._last_reboot_at: dict[str, float] = {}
        self._per_device: dict[str, deque[float]] = {}

    def can_reboot(self, mac: str, *, now: float) -> tuple[bool, str | None, float | None]:
        """Returns (allowed, reason, retry_after_seconds). reason is 'cooldown'
        when the per-device single-flight window has not elapsed, 'daily_cap'
        when the rolling-24h reboot count is at the limit."""
        last = self._last_reboot_at.get(mac)
        if self.per_device_seconds > 0 and last is not None:
            elapsed = now - last
            if elapsed < self.per_device_seconds:
                return False, "cooldown", self.per_device_seconds - elapsed
        if self.max_per_device_per_day > 0:
            window = self._prune(mac, now)
            if len(window) >= self.max_per_device_per_day:
                # retry_after = when the oldest reboot in the window ages out.
                return False, "daily_cap", _DAY_SECONDS - (now - window[0])
        return True, None, None

    def record_reboot(self, mac: str, *, now: float) -> None:
        self._last_reboot_at[mac] = now
        # Only track the per-device window when the cap is active, so the deque
        # cannot grow unbounded when the cap is off (matches ADR-0004 record_kick).
        if self.max_per_device_per_day > 0:
            self._per_device.setdefault(mac, deque()).append(now)

    def update_params(self, *, per_device_seconds: int, max_per_device_per_day: int) -> None:
        # ADR-0006 AC-12 (ADR-0004 AC-8 posture): SIGHUP retunes the windows in
        # place; in-flight state (_last_reboot_at, _per_device) is NOT purged.
        self.per_device_seconds = per_device_seconds
        self.max_per_device_per_day = max_per_device_per_day

    def _prune(self, mac: str, now: float) -> deque[float]:
        """Drop reboots older than the 24h window. Returns the (mutated) deque."""
        window = self._per_device.setdefault(mac, deque())
        cutoff = now - _DAY_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        return window
