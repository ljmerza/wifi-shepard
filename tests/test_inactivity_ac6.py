"""ADR-0010 AC-6: a counter reset (negative delta = the client reassociated/
rebooted) or None counters (backend didn't report them) clears the window — no
flag. Both are fail-safe: activity, or "can't evaluate", never a flatline.
"""

from __future__ import annotations

from tests.conftest import make_client
from wifi_shepard.config import build_config
from wifi_shepard.inactivity import InactivityScorer

MAC = "34:ea:e7:11:22:33"


def _scorer(window_samples=3, min_bytes=1000):
    config = build_config(
        inactivity=dict(
            enabled=True,
            min_bytes_per_window=min_bytes,
            window_samples=window_samples,
            macs=[MAC],
        )
    )
    return InactivityScorer(config)


def _ingest(scorer, total):
    # Split an arbitrary cumulative total across tx/rx.
    return scorer.ingest(make_client(mac=MAC, tx_bytes=total, rx_bytes=0))


def test_negative_delta_clears_window_no_flag():
    scorer = _scorer(window_samples=3)
    # baseline + 2 flat deltas → window=[0,0], one short of a full window.
    assert _ingest(scorer, 1000) is None  # baseline
    assert _ingest(scorer, 1000) is None  # delta 0
    assert _ingest(scorer, 1000) is None  # delta 0
    # Counter reset: total drops → negative delta → window cleared, no flag.
    assert _ingest(scorer, 200) is None
    # A single flat sample right after the reset must NOT immediately flag — the
    # window was cleared, so it needs a fresh full run.
    assert _ingest(scorer, 200) is None  # fresh window=[0], len 1
    assert _ingest(scorer, 200) is None  # window=[0,0], len 2
    assert _ingest(scorer, 200) is not None, "fresh full flat window flags after the reset"


def test_none_counters_clear_window_no_flag():
    scorer = _scorer(window_samples=2)
    assert _ingest(scorer, 5000) is None  # baseline
    assert _ingest(scorer, 5000) is None  # delta 0, window=[0], len 1
    # None counters mid-stream: cannot evaluate → clear, no flag.
    assert scorer.ingest(make_client(mac=MAC, tx_bytes=None, rx_bytes=None)) is None
    # Only rx present is still "cannot evaluate".
    assert scorer.ingest(make_client(mac=MAC, tx_bytes=None, rx_bytes=5000)) is None
    # Counters resume: must re-establish a baseline and a fresh full window.
    assert _ingest(scorer, 5000) is None  # new baseline (prev was cleared)
    assert _ingest(scorer, 5000) is None  # window=[0], len 1
    assert _ingest(scorer, 5000) is not None, "fresh full flat window flags once counters resume"
