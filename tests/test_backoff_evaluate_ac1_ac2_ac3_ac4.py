"""ADR-0007 AC-1/AC-2/AC-3/AC-4: the pure per-MAC backoff gate (evaluate_backoff).

Deterministic unit tests over kick timestamps + a fixed `now`; no DB or clock.
"""

from __future__ import annotations

from wifi_shepard.backoff import evaluate_backoff

COOLDOWNS = (300, 1800, 7200, 43200, 86400)
NOW = 1_000_000.0


def test_ac1_cooldown_blocks_then_allows():
    # One prior kick 200s ago; the first-rung cooldown is 300s -> blocked.
    allowed, reason, retry = evaluate_backoff(
        [NOW - 200], NOW, cooldowns=COOLDOWNS, max_per_hour=0, max_per_day=0
    )
    assert allowed is False
    assert reason == "per_mac_cooldown"
    assert retry == 100.0  # 300 - 200

    # Same single kick 300s ago -> cooldown satisfied -> allowed.
    allowed, reason, retry = evaluate_backoff(
        [NOW - 300], NOW, cooldowns=COOLDOWNS, max_per_hour=0, max_per_day=0
    )
    assert allowed is True
    assert reason is None
    assert retry is None


def test_ac2_cooldown_escalates_with_run_length_and_clamps():
    # N prior kicks in the recovery window -> cooldown = COOLDOWNS[min(N-1, len-1)].
    # N=7 must clamp to the last rung (index 4).
    for n, expected_idx in [(1, 0), (2, 1), (3, 2), (4, 3), (5, 4), (7, 4)]:
        recent = sorted(NOW - 10 * (n - i) for i in range(n))  # all < 1h old
        allowed, reason, retry = evaluate_backoff(
            recent, NOW, cooldowns=COOLDOWNS, max_per_hour=0, max_per_day=0
        )
        assert reason == "per_mac_cooldown", f"n={n} should be cooling down"
        elapsed = NOW - max(recent)
        assert abs(retry - (COOLDOWNS[expected_idx] - elapsed)) < 1e-6, (
            f"n={n}: expected cooldown index {expected_idx} ({COOLDOWNS[expected_idx]}s)"
        )


def test_ac2_escalation_resets_after_recovery_window():
    # A lone kick older than the longest cooldown (recovery window) is out of the
    # run, so the next kick starts again at the first rung -> allowed.
    allowed, reason, _ = evaluate_backoff(
        [NOW - (86400 + 1)], NOW, cooldowns=COOLDOWNS, max_per_hour=0, max_per_day=0
    )
    assert allowed is True
    assert reason is None


def test_ac3_hourly_cap_blocks_fourth():
    recent = [NOW - 1800, NOW - 1200, NOW - 600]  # 3 within the last hour
    allowed, reason, retry = evaluate_backoff(
        recent, NOW, cooldowns=(), max_per_hour=3, max_per_day=0
    )
    assert allowed is False
    assert reason == "per_mac_hourly_cap"
    assert retry == 1800.0  # 3600 - (now - oldest-in-hour)

    # Only 2 within the hour -> allowed.
    allowed, reason, _ = evaluate_backoff(
        [NOW - 1200, NOW - 600], NOW, cooldowns=(), max_per_hour=3, max_per_day=0
    )
    assert allowed is True


def test_ac4_daily_cap_blocks_eleventh():
    recent = [NOW - 3600 * i for i in range(1, 11)]  # 10 kicks over the last 10h
    allowed, reason, _ = evaluate_backoff(recent, NOW, cooldowns=(), max_per_hour=0, max_per_day=10)
    assert allowed is False
    assert reason == "per_mac_daily_cap"

    # 9 within the day -> allowed.
    allowed, reason, _ = evaluate_backoff(
        recent[:9], NOW, cooldowns=(), max_per_hour=0, max_per_day=10
    )
    assert allowed is True


def test_daily_cap_takes_precedence_over_hourly_and_cooldown():
    # All three would trip; daily is checked first and names itself.
    recent = [NOW - 60 * i for i in range(1, 11)]  # 10 kicks, all recent
    _, reason, _ = evaluate_backoff(
        recent, NOW, cooldowns=COOLDOWNS, max_per_hour=3, max_per_day=10
    )
    assert reason == "per_mac_daily_cap"


def test_all_limits_off_allows():
    allowed, reason, retry = evaluate_backoff(
        [NOW - 1], NOW, cooldowns=(), max_per_hour=0, max_per_day=0
    )
    assert allowed is True
    assert reason is None
    assert retry is None
