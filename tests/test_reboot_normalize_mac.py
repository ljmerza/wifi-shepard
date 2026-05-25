"""ADR-0005: normalize_mac is the canonical MAC form every reboot surface
(eligibility, override match, OUI pre-filter) compares against. It must make
MACs that differ only in case or surrounding whitespace compare equal, so an
operator's `AA:BB:CC:DD:EE:FF` in one block matches `aa:bb:cc:dd:ee:ff` in
another. Tested directly because it underpins every other reboot comparison.
"""

from __future__ import annotations

from wifi_shepard.reboot import normalize_mac


def test_lowercase_passthrough() -> None:
    assert normalize_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"


def test_uppercase_lowered() -> None:
    assert normalize_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"


def test_mixed_case_lowered() -> None:
    assert normalize_mac("Aa:Bb:Cc:Dd:Ee:Ff") == "aa:bb:cc:dd:ee:ff"


def test_surrounding_whitespace_stripped() -> None:
    assert normalize_mac("  aa:bb:cc:dd:ee:ff\n") == "aa:bb:cc:dd:ee:ff"


def test_case_and_whitespace_only_differences_compare_equal() -> None:
    a = normalize_mac("  AA:BB:CC:DD:EE:FF ")
    b = normalize_mac("aa:bb:cc:dd:ee:ff")
    assert a == b
