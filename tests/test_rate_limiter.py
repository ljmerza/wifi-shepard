"""Unit tests for KickRateLimiter (ADR-0004 Phase 0).

Structural tests only — no AC labels. AC tests live with the actor wiring
(test_rate_limit_*.py). Verifies the limiter's two-method API per ADR-0004
Fork K:
- record_kick(ap_id, now): updates both global timestamp and per-AP deque (fresh kick).
- record_wire_call(now): updates only the global timestamp (deauth_fallback).
"""

from __future__ import annotations

import pytest

from wifi_shepard.rate_limit import KickRateLimiter


def _make(
    *,
    min_seconds_between_kicks: int = 0,
    max_kicks_per_ap_per_window: int = 0,
    per_ap_window_seconds: int = 600,
) -> KickRateLimiter:
    return KickRateLimiter(
        min_seconds_between_kicks=min_seconds_between_kicks,
        max_kicks_per_ap_per_window=max_kicks_per_ap_per_window,
        per_ap_window_seconds=per_ap_window_seconds,
    )


def test_default_off_allows_every_call() -> None:
    rl = _make()  # both limits = 0 (off)
    for t in range(0, 1000, 7):
        allowed, reason, retry = rl.can_kick("ap1", now=float(t))
        assert allowed is True
        assert reason is None
        assert retry is None


def test_global_single_flight_allows_first_kick() -> None:
    rl = _make(min_seconds_between_kicks=30)
    allowed, reason, retry = rl.can_kick("ap1", now=0.0)
    assert allowed is True
    assert reason is None
    assert retry is None


def test_global_single_flight_blocks_within_window() -> None:
    rl = _make(min_seconds_between_kicks=30)
    rl.record_kick("ap1", now=0.0)
    allowed, reason, retry = rl.can_kick("ap2", now=5.0)
    assert allowed is False
    assert reason == "global_rate_limit"
    assert retry == pytest.approx(25.0)


def test_global_single_flight_releases_at_threshold() -> None:
    rl = _make(min_seconds_between_kicks=30)
    rl.record_kick("ap1", now=0.0)
    allowed, _, _ = rl.can_kick("ap1", now=30.0)
    assert allowed is True


def test_per_ap_cap_blocks_third_in_window() -> None:
    rl = _make(max_kicks_per_ap_per_window=2, per_ap_window_seconds=600)
    rl.record_kick("ap1", now=0.0)
    rl.record_kick("ap1", now=10.0)
    allowed, reason, retry = rl.can_kick("ap1", now=20.0)
    assert allowed is False
    assert reason == "per_ap_cap"
    assert retry == pytest.approx(580.0)  # 600 - (20 - 0) = oldest kick expires at 600


def test_per_ap_cap_releases_after_window_expires() -> None:
    rl = _make(max_kicks_per_ap_per_window=2, per_ap_window_seconds=600)
    rl.record_kick("ap1", now=0.0)
    rl.record_kick("ap1", now=10.0)
    # At t=601, the t=0 kick has aged out; only the t=10 kick remains in the window.
    allowed, _, _ = rl.can_kick("ap1", now=601.0)
    assert allowed is True


def test_per_ap_cap_is_isolated_by_ap() -> None:
    rl = _make(max_kicks_per_ap_per_window=2, per_ap_window_seconds=600)
    rl.record_kick("ap1", now=0.0)
    rl.record_kick("ap1", now=10.0)
    # ap1 capped; ap2 untouched.
    allowed_a, _, _ = rl.can_kick("ap1", now=20.0)
    allowed_b, _, _ = rl.can_kick("ap2", now=20.0)
    assert allowed_a is False
    assert allowed_b is True


def test_global_check_runs_before_per_ap_check() -> None:
    # Both limits configured; global should report first because it's the cheaper check
    # and the operator's first-line defense is the global lockout-prevention knob.
    rl = _make(min_seconds_between_kicks=30, max_kicks_per_ap_per_window=2)
    rl.record_kick("ap1", now=0.0)
    rl.record_kick("ap1", now=10.0)
    # ap1 is at per-AP cap AND we're 5s after the last kick. Global wins.
    allowed, reason, _ = rl.can_kick("ap1", now=15.0)
    assert allowed is False
    assert reason == "global_rate_limit"


def test_record_wire_call_updates_global_only() -> None:
    rl = _make(min_seconds_between_kicks=30, max_kicks_per_ap_per_window=2)
    rl.record_kick("ap1", now=0.0)  # global=0, ap1=[0]
    rl.record_wire_call(now=60.0)  # global=60, ap1 unchanged
    # Per-AP deque must still have only 1 entry (so 2-cap is not yet reached).
    # Verify by checking we can kick ap1 again past the global window.
    allowed, _, _ = rl.can_kick("ap1", now=100.0)
    assert allowed is True
    # But within the global window (60 + 30 = 90), still blocked.
    allowed2, reason2, _ = rl.can_kick("ap1", now=80.0)
    assert allowed2 is False
    assert reason2 == "global_rate_limit"


def test_record_kick_updates_both_counters() -> None:
    rl = _make(min_seconds_between_kicks=30, max_kicks_per_ap_per_window=1)
    rl.record_kick("ap1", now=0.0)  # global=0, ap1=[0]; cap is 1 so ap1 is now full
    # Past global window:
    allowed, reason, _ = rl.can_kick("ap1", now=100.0)
    assert allowed is False
    assert reason == "per_ap_cap"


def test_can_wire_call_gates_global_only_not_per_ap() -> None:
    """can_wire_call (fallback path) is gated by min_seconds_between_kicks only.
    The per-AP cap does not apply — fallback is part of the same attempt_group
    that was already counted at the BTM stage (ADR-0004 Fork G)."""
    rl = _make(min_seconds_between_kicks=30, max_kicks_per_ap_per_window=1)
    rl.record_kick("ap1", now=0.0)  # ap1 deque now [0]; cap=1 means ap1 is full
    # Global window is open at t=100. Per-AP would block can_kick, but can_wire_call ignores it.
    allowed, reason, _ = rl.can_wire_call(now=100.0)
    assert allowed is True
    assert reason is None
    # And inside the global window it does block:
    allowed2, reason2, _ = rl.can_wire_call(now=15.0)
    assert allowed2 is False
    assert reason2 == "global_rate_limit"


def test_per_ap_deque_prunes_old_entries() -> None:
    # Verify the per-AP deque doesn't grow unboundedly.
    rl = _make(max_kicks_per_ap_per_window=10, per_ap_window_seconds=100)
    for t in range(0, 500, 5):  # 100 kicks over 500s
        rl.record_kick("ap1", now=float(t))
    # After all those kicks, only entries within the last 100s should be counted.
    # The internal deque should not contain entries older than now - window.
    # We assert via behavior: at t=500, can_kick should reflect ≤10 entries in window.
    # range(0, 500, 5) → 100 entries at t=0..495 step 5. At now=500 with 100s window,
    # "in window" means t >= 400 → entries at 400, 405, ..., 495 → 20 entries.
    # Cap is 10, so we expect blocked with per_ap_cap.
    allowed, reason, _ = rl.can_kick("ap1", now=500.0)
    assert allowed is False
    assert reason == "per_ap_cap"
    # And the deque length is bounded by entries-in-window, not all-time.
    assert len(rl._per_ap_kicks["ap1"]) <= 20  # exactly the entries in window after prune
