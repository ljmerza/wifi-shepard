"""Unit tests for PendingKicks — the in-flight kick bookkeeping extracted from
Actor (SOLID review M1). The maps are a pure relocation of state, so these tests
pin the exact dict shapes and the consume-once semantics the Actor relies on.
"""

from __future__ import annotations

from wifi_shepard.pending import PendingKicks


def test_btm_set_get_has_clear_roundtrip() -> None:
    pk = PendingKicks()
    assert pk.get_btm("aa") is None
    assert not pk.has_btm("aa")

    pk.set_btm("aa", group="g1", ap_id="ap1")
    assert pk.has_btm("aa")
    # Shape must stay {"group", "ap_id"} — handle() reads pending["group"]/["ap_id"].
    assert pk.get_btm("aa") == {"group": "g1", "ap_id": "ap1"}

    pk.clear_btm("aa")
    assert not pk.has_btm("aa")
    assert pk.get_btm("aa") is None


def test_clear_btm_absent_mac_is_noop() -> None:
    # Mirrors the old dict.pop(mac, None): clearing an unknown MAC must not raise,
    # because check_post_kick_outcome() clears unconditionally on a successful roam.
    pk = PendingKicks()
    pk.clear_btm("missing")
    assert not pk.has_btm("missing")


def test_outcome_pop_consumes_once() -> None:
    pk = PendingKicks()
    assert pk.pop_outcome("aa") is None

    pk.set_outcome("aa", ap_id="ap1", mechanism="deauth", attempt_group="g1")
    assert pk.pop_outcome("aa") == {
        "ap_id": "ap1",
        "mechanism": "deauth",
        "attempt_group": "g1",
    }
    # The post-kick check runs once per cycle and pops; a second pop must be empty.
    assert pk.pop_outcome("aa") is None


def test_btm_and_outcome_maps_are_independent() -> None:
    pk = PendingKicks()
    pk.set_btm("aa", group="g1", ap_id="ap1")
    pk.set_outcome("aa", ap_id="ap1", mechanism="btm", attempt_group="g1")

    # Consuming the outcome must not disturb the pending BTM (separate maps).
    pk.pop_outcome("aa")
    assert pk.has_btm("aa")

    # ...and clearing the BTM must not resurrect the already-consumed outcome.
    pk.clear_btm("aa")
    assert pk.pop_outcome("aa") is None
