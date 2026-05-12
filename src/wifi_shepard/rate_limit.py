"""Kick rate limiter — global single-flight + per-AP cap (ADR-0004).

Both limits are opt-in: a value of 0 means "off". The limiter has no notion of
wall-clock time; callers pass in `now` (typically `time.monotonic()`), so tests
can simulate clock advancement without monkey-patching the `time` module.

Two-method record API (ADR-0004 Fork K):
- record_kick(ap_id, now)    -> fresh kick; updates global + per-AP.
- record_wire_call(now)      -> deauth_fallback wire call under the same
                                attempt_group; updates global only.

State is in-memory and lost on restart (ADR-0004 Decision Fork A); per-MAC
backoff and per-day kick caps bound the worst-case cold-start burst.
"""

from __future__ import annotations

from collections import deque


class KickRateLimiter:
    def __init__(
        self,
        *,
        min_seconds_between_kicks: int,
        max_kicks_per_ap_per_window: int,
        per_ap_window_seconds: int,
    ) -> None:
        self.min_seconds_between_kicks = min_seconds_between_kicks
        self.max_kicks_per_ap_per_window = max_kicks_per_ap_per_window
        self.per_ap_window_seconds = per_ap_window_seconds
        self._last_kick_at: float | None = None
        self._per_ap_kicks: dict[str, deque[float]] = {}

    def _check_global(self, now: float) -> tuple[bool, str | None, float | None]:
        if self.min_seconds_between_kicks > 0 and self._last_kick_at is not None:
            elapsed = now - self._last_kick_at
            if elapsed < self.min_seconds_between_kicks:
                return False, "global_rate_limit", self.min_seconds_between_kicks - elapsed
        return True, None, None

    def can_kick(self, ap_id: str, *, now: float) -> tuple[bool, str | None, float | None]:
        """Fresh-kick gate: global single-flight + per-AP cap.

        Global is checked first so the operator's first-line lockout-prevention
        knob always wins when both trip — global retry_after is shorter and
        self-correcting.
        """
        allowed, reason, retry = self._check_global(now)
        if not allowed:
            return False, reason, retry
        if self.max_kicks_per_ap_per_window > 0:
            deq = self._prune(ap_id, now)
            if len(deq) >= self.max_kicks_per_ap_per_window:
                # retry_after = when the oldest in-window kick falls out of the window.
                retry = self.per_ap_window_seconds - (now - deq[0])
                return False, "per_ap_cap", retry
        return True, None, None

    def can_wire_call(self, *, now: float) -> tuple[bool, str | None, float | None]:
        """Fallback wire-call gate: global single-flight only.

        A deauth_fallback under an existing attempt_group is the same logical
        kick that already passed the per-AP check at the BTM stage (ADR-0004
        Fork G). Only the global timer can still trip the wire call.
        """
        return self._check_global(now)

    def record_kick(self, ap_id: str, *, now: float) -> None:
        """Fresh kick: both the global single-flight timer and the per-AP window count it."""
        self._last_kick_at = now
        # Only track per-AP state when the cap is active. Pruning is gated on the
        # same condition inside can_kick, so an always-on append with the cap off
        # would grow the deque unboundedly over the daemon's lifetime.
        if self.max_kicks_per_ap_per_window > 0:
            self._per_ap_kicks.setdefault(ap_id, deque()).append(now)

    def record_wire_call(self, *, now: float) -> None:
        """Wire call under an existing attempt_group (deauth_fallback). Global only."""
        self._last_kick_at = now

    def _prune(self, ap_id: str, now: float) -> deque[float]:
        """Drop per-AP entries older than the window. Returns the (mutated) deque."""
        deq = self._per_ap_kicks.setdefault(ap_id, deque())
        cutoff = now - self.per_ap_window_seconds
        while deq and deq[0] < cutoff:
            deq.popleft()
        return deq
