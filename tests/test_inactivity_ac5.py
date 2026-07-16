"""ADR-0010 AC-5: a MAC not in `macs` is never flagged regardless of deltas; an
allowlisted MAC is never flagged even when opted in, and config load warns.
"""

from __future__ import annotations

import logging

from tests.conftest import make_client
from wifi_shepard.config import build_config
from wifi_shepard.inactivity import InactivityScorer

OPTED = "34:ea:e7:11:22:33"
OTHER = "aa:bb:cc:dd:ee:ff"


def _feed_flat(scorer: InactivityScorer, mac: str, n: int):
    """Feed n flat (zero-delta) samples, returning the list of ingest decisions."""
    out = []
    for _ in range(n):
        out.append(scorer.ingest(make_client(mac=mac, tx_bytes=1000, rx_bytes=2000)))
    return out


def test_mac_not_in_opt_in_set_never_flagged():
    config = build_config(
        inactivity=dict(enabled=True, min_bytes_per_window=10_000, window_samples=2, macs=[OPTED])
    )
    scorer = InactivityScorer(config)
    # A non-opted MAC, flatlined well past a full window, is never evaluated.
    decisions = _feed_flat(scorer, OTHER, 6)
    assert all(d is None for d in decisions)


def test_allowlisted_opted_in_mac_never_flagged():
    # OPTED is in BOTH macs and allowlist → allowlist wins (defense in depth).
    config = build_config(
        inactivity=dict(enabled=True, min_bytes_per_window=10_000, window_samples=2, macs=[OPTED]),
        allowlist=[OPTED],
    )
    scorer = InactivityScorer(config)
    decisions = _feed_flat(scorer, OPTED, 6)
    assert all(d is None for d in decisions), "allowlisted MAC must never be flagged"


def test_config_load_warns_on_inactivity_mac_in_allowlist(caplog):
    with caplog.at_level(logging.WARNING, logger="wifi_shepard"):
        build_config(
            inactivity=dict(enabled=True, macs=[OPTED]),
            allowlist=[OPTED],
        )
    warned = [
        getattr(r, "mac", None)
        for r in caplog.records
        if r.getMessage() == "inactivity_mac_in_allowlist"
    ]
    assert OPTED in warned, f"expected inactivity_mac_in_allowlist for {OPTED}; got {warned}"


def test_no_warning_when_opted_mac_not_allowlisted(caplog):
    with caplog.at_level(logging.WARNING, logger="wifi_shepard"):
        build_config(inactivity=dict(enabled=True, macs=[OPTED]), allowlist=[OTHER])
    warned = [
        getattr(r, "mac", None)
        for r in caplog.records
        if r.getMessage() == "inactivity_mac_in_allowlist"
    ]
    assert warned == []


def test_opted_mac_does_flag_as_control():
    # Control: the SAME opted, non-allowlisted MAC DOES flag when flatlined, proving
    # the "never flagged" assertions above are about the gate, not a dead detector.
    config = build_config(
        inactivity=dict(enabled=True, min_bytes_per_window=10_000, window_samples=2, macs=[OPTED])
    )
    scorer = InactivityScorer(config)
    decisions = _feed_flat(scorer, OPTED, 3)  # baseline + 2 flat deltas
    assert any(d is not None for d in decisions), "opted, non-allowlisted MAC must flag"
