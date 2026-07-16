"""ADR-0010 AC-8: SIGHUP-style update_config picks up changed inactivity settings.

Mirrors the Scorer reload convention (tests/test_pipeline.py, ADR-0004 AC-8):
a change to the inactivity scorer's OWN window_samples rebuilds it (deque maxlen
is fixed at construction); any other change (min_bytes_per_window) applies in
place so the accumulated per-MAC byte windows survive the reload.
"""

from __future__ import annotations

from typing import Any

from tests.conftest import FakeController, FakeHANotifier, make_client
from wifi_shepard.config import build_config
from wifi_shepard.inactivity import InactivityScorer
from wifi_shepard.pipeline import build_pipeline

MAC = "34:ea:e7:11:22:33"


class _FakeStore:
    async def insert_sample(self, client: Any) -> None:  # pragma: no cover - unused
        return None

    async def insert_kick(self, **kwargs: Any) -> None:  # pragma: no cover - unused
        return None


def _pipeline(window_samples=2, min_bytes=100):
    config = build_config(
        inactivity=dict(
            enabled=True,
            min_bytes_per_window=min_bytes,
            window_samples=window_samples,
            macs=[MAC],
        )
    )
    return build_pipeline(config, controller=FakeController(), db=_FakeStore(), ha=FakeHANotifier())


def _ingest(scorer: InactivityScorer, total: int):
    return scorer.ingest(make_client(mac=MAC, tx_bytes=total, rx_bytes=0))


def test_window_size_change_rebuilds_inactivity_scorer():
    pipeline = _pipeline(window_samples=2)
    before = pipeline.inactivity
    new_config = build_config(
        inactivity=dict(enabled=True, min_bytes_per_window=100, window_samples=5, macs=[MAC])
    )
    pipeline.update_config(new_config)
    assert pipeline.inactivity is not before, "window_samples change must rebuild the scorer"
    assert pipeline.inactivity.config is new_config


def test_threshold_change_applies_in_place_preserving_window():
    pipeline = _pipeline(window_samples=2, min_bytes=100)
    scorer_before = pipeline.inactivity

    # Accumulate a full window of +100/poll deltas: sum=200, which is NOT below the
    # old floor of 100 → no flag yet.
    assert _ingest(pipeline.inactivity, 0) is None  # baseline
    assert _ingest(pipeline.inactivity, 100) is None  # window=[100]
    assert _ingest(pipeline.inactivity, 200) is None  # window=[100,100], sum 200 >= 100

    # SIGHUP-style reload: same window_samples, higher floor (500).
    new_config = build_config(
        inactivity=dict(enabled=True, min_bytes_per_window=500, window_samples=2, macs=[MAC])
    )
    pipeline.update_config(new_config)
    assert pipeline.inactivity is scorer_before, "same window_samples must swap config in place"
    assert pipeline.inactivity.config is new_config

    # The accumulated window survived the reload: a SINGLE further poll completes it
    # under the NEW floor (sum 200 < 500 → flag). Had the reload rebuilt the scorer,
    # the window would be empty and one poll could not fill a maxlen-2 window.
    decision = _ingest(pipeline.inactivity, 300)  # delta 100, window=[100,100], sum 200
    assert decision is not None, "in-place reload must preserve the accumulated byte window"
    assert decision["min_bytes_per_window"] == 500, "the new floor must be the live threshold"
