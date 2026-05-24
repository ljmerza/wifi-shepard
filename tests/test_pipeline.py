"""Unit tests for the detection pipeline (SOLID review H2).

Covers the two pieces extracted out of Scanner:
- ``build_pipeline`` — the single place that knows construction order; assert it
  threads config into the collaborators and shares the same backoff/rate-limiter
  instances into the Actor (no silent duplication).
- ``DetectionPipeline.update_config`` — the SIGHUP reload. Two branches: rebuild
  the scorer only when the window size changes; otherwise mutate in place so
  accumulated per-MAC state (sample windows, kick timers, backoff counts) survives
  the reload (ADR-0004 AC-8).
"""

from __future__ import annotations

from typing import Any

from tests.conftest import FakeController, FakeHANotifier, make_client
from wifi_shepard.actor import Actor
from wifi_shepard.backoff import BackoffManager
from wifi_shepard.config import build_config
from wifi_shepard.pipeline import DetectionPipeline, build_pipeline
from wifi_shepard.rate_limit import KickRateLimiter
from wifi_shepard.scorer import Scorer


class _FakeStore:
    """Minimal Store: build_pipeline only stores the ref, nothing calls it here."""

    async def insert_sample(self, client: Any) -> None:  # pragma: no cover - unused
        return None

    async def insert_kick(self, **kwargs: Any) -> None:  # pragma: no cover - unused
        return None


def _build() -> DetectionPipeline:
    config = build_config(
        window_samples=5,
        quarantine_after_kicks=3,
        safety_rails=dict(
            min_seconds_between_kicks=10,
            max_kicks_per_ap_per_window=3,
            per_ap_window_seconds=600,
        ),
    )
    return build_pipeline(config, controller=FakeController(), db=_FakeStore(), ha=FakeHANotifier())


def test_build_pipeline_threads_config_and_shares_instances() -> None:
    pipeline = _build()

    assert isinstance(pipeline.scorer, Scorer)
    assert isinstance(pipeline.backoff, BackoffManager)
    assert isinstance(pipeline.rate_limiter, KickRateLimiter)
    assert isinstance(pipeline.actor, Actor)

    # Config values threaded through to the collaborators.
    assert pipeline.backoff.quarantine_after_kicks == 3
    assert pipeline.rate_limiter.min_seconds_between_kicks == 10
    assert pipeline.rate_limiter.max_kicks_per_ap_per_window == 3
    assert pipeline.rate_limiter.per_ap_window_seconds == 600

    # The Actor must receive the *same* backoff/rate-limiter instances the pipeline
    # holds, not freshly constructed duplicates — otherwise reload-in-place updates
    # would touch one copy while the Actor reads another.
    assert pipeline.actor.backoff is pipeline.backoff
    assert pipeline.actor.rate_limiter is pipeline.rate_limiter


def test_update_config_preserves_scorer_when_window_unchanged() -> None:
    pipeline = _build()
    scorer_before = pipeline.scorer
    pipeline.scorer.ingest(make_client(mac="aa:bb:cc:dd:ee:ff"))

    # Same window_samples, different detection threshold.
    new_config = build_config(window_samples=5, signal_dbm_max=-60)
    pipeline.update_config(new_config)

    # Same object => the accumulated per-MAC sample window survives the reload.
    assert pipeline.scorer is scorer_before
    assert pipeline.scorer.config is new_config


def test_update_config_rebuilds_scorer_when_window_changes() -> None:
    pipeline = _build()
    scorer_before = pipeline.scorer

    new_config = build_config(window_samples=9)
    pipeline.update_config(new_config)

    # Window size is baked into the deque maxlen at construction, so the scorer is
    # rebuilt (a fresh object) rather than mutated.
    assert pipeline.scorer is not scorer_before
    assert pipeline.scorer.config is new_config


def test_update_config_updates_limits_in_place_without_resetting_state() -> None:
    pipeline = _build()

    # Seed in-flight state: a recorded kick (global timer + per-AP window) and a
    # backoff count for the same MAC.
    pipeline.rate_limiter.record_kick("ap1", now=100.0)
    pipeline.backoff.record_kick("aa:bb:cc:dd:ee:ff")
    assert pipeline.rate_limiter._last_kick_at == 100.0
    assert pipeline.backoff.kick_count("aa:bb:cc:dd:ee:ff") == 1

    rate_limiter_before = pipeline.rate_limiter
    backoff_before = pipeline.backoff

    new_config = build_config(
        window_samples=5,
        quarantine_after_kicks=7,
        safety_rails=dict(
            min_seconds_between_kicks=20,
            max_kicks_per_ap_per_window=5,
            per_ap_window_seconds=300,
        ),
    )
    pipeline.update_config(new_config)

    # Same instances, mutated in place (ADR-0004 AC-8: tuning, not a state purge).
    assert pipeline.rate_limiter is rate_limiter_before
    assert pipeline.backoff is backoff_before

    # New thresholds applied...
    assert pipeline.rate_limiter.min_seconds_between_kicks == 20
    assert pipeline.rate_limiter.max_kicks_per_ap_per_window == 5
    assert pipeline.rate_limiter.per_ap_window_seconds == 300
    assert pipeline.backoff.quarantine_after_kicks == 7
    assert pipeline.actor.config is new_config

    # ...but the in-flight state is untouched.
    assert pipeline.rate_limiter._last_kick_at == 100.0
    assert list(pipeline.rate_limiter._per_ap_kicks["ap1"]) == [100.0]
    assert pipeline.backoff.kick_count("aa:bb:cc:dd:ee:ff") == 1
