"""The allowlist matches MACs case- and whitespace-insensitively, and fails closed
on a malformed entry.

The allowlist is the daemon's primary safety control: the one gate that says "never
touch this device". It was an exact-match test against the raw YAML string, so an
operator who wrote a MAC in uppercase (the form printed on most device labels) got an
entry that silently never matched, and the device it was meant to protect kept getting
kicked. Every other MAC comparison in the repo already normalizes — reboot/eligibility.py,
reboot/oui.py, reboot/ha_resolver.py, and the UI's COLLATE NOCASE, whose comment notes
that "aiounifi and other backends normalize MAC case inconsistently across firmware
versions". These tests close that gap on the comparison that decides whether to deauth.

A malformed entry now raises at load rather than silently never matching, mirroring the
fail-closed posture reboot.eligible already has (config.py `_is_valid_mac`).
"""

from __future__ import annotations

import logging

import pytest

from tests.conftest import FakeController, make_client


def _bad_client(mac: str):
    """A client that scores bad-state on every criterion, so anything short of an
    allowlist hit results in a kick."""
    return make_client(
        mac=mac,
        signal=-80,
        tx_rate_kbps=4000,
        tx_retries=60,
        wifi_tx_attempts=100,
        radio="ng",
    )


async def _run_once(temp_db_path, fake_ha, config, client):
    from wifi_shepard.db import Database
    from wifi_shepard.scanner import Scanner

    fake = FakeController(clients=[client])
    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(
            controller=fake,
            db=db,
            poll_interval_seconds=0.001,
            config=config,
            ha=fake_ha,
        )
        await scanner.run_once()
    finally:
        await db.close()
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listed", "reported"),
    [
        # The reported bug: operator copies the MAC off the device label in uppercase.
        ("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"),
        # The mirror image: a firmware update starts reporting uppercase MACs while
        # the config holds the lowercase form the operator was told to use.
        ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"),
        # Mixed case on both sides.
        ("Aa:Bb:Cc:Dd:Ee:Ff", "aA:bB:cC:dD:eE:fF"),
        # Stray whitespace from a hand-edited YAML list.
        ("  aa:bb:cc:dd:ee:ff  ", "aa:bb:cc:dd:ee:ff"),
    ],
)
async def test_allowlist_matches_regardless_of_mac_case(
    temp_db_path, fake_ha, caplog, listed, reported
):
    from wifi_shepard.config import build_config

    config = build_config(dry_run=False, window_samples=1, allowlist=[listed])

    with caplog.at_level(logging.INFO, logger="wifi_shepard"):
        fake = await _run_once(temp_db_path, fake_ha, config, _bad_client(reported))

    assert fake.force_reconnect_calls == [], (
        f"allowlist entry {listed!r} must protect a client reported as {reported!r}; "
        f"the allowlist is a safety control and must not fail open on MAC casing"
    )
    assert fake_ha.posts == [], "an allowlisted MAC must never trigger an HA notify"
    kick_logs = [r for r in caplog.records if r.getMessage() in ("kick", "would_kick")]
    assert kick_logs == [], f"an allowlisted MAC must never log a kick; got {kick_logs}"


@pytest.mark.asyncio
async def test_non_allowlisted_mac_is_still_kicked(temp_db_path, fake_ha):
    """Control: normalization must not turn the allowlist into a catch-all. Without
    this, every assertion above would pass even if the gate rejected everything."""
    from wifi_shepard.config import build_config

    config = build_config(dry_run=False, window_samples=1, allowlist=["AA:BB:CC:DD:EE:FF"])
    fake = await _run_once(temp_db_path, fake_ha, config, _bad_client("11:22:33:44:55:66"))

    assert fake.force_reconnect_calls == ["11:22:33:44:55:66"], (
        "a MAC that is not on the allowlist must still be kicked"
    )


def test_allowlist_is_normalized_at_load():
    from wifi_shepard.config import build_config

    config = build_config(allowlist=["  AA:BB:CC:DD:EE:FF  ", "11:22:33:44:55:66"])

    assert config.allowlist == ("aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"), (
        "allowlist entries must be stored in canonical (stripped, lowercase) form so "
        "every consumer compares against one representation"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-mac",
        "aa:bb:cc:dd:ee",  # five octets
        "aa:bb:cc:dd:ee:ff:00",  # seven octets
        "aabbccddeeff",  # bare hex, no separators
        "aa-bb-cc-dd-ee-ff",  # dash form
        "zz:bb:cc:dd:ee:ff",  # non-hex
        "",
    ],
)
def test_malformed_allowlist_entry_fails_closed(bad):
    """A typo'd allowlist entry used to be silently inert — it just never matched, and
    the device it was meant to protect kept getting kicked with no signal. Fail closed
    at load instead, the way reboot.eligible already does."""
    from wifi_shepard.config import build_config

    with pytest.raises(ValueError, match="allowlist"):
        build_config(allowlist=[bad])


def test_malformed_allowlist_error_names_the_offending_entry():
    from wifi_shepard.config import build_config

    with pytest.raises(ValueError, match=r"allowlist\[1\]"):
        build_config(allowlist=["aa:bb:cc:dd:ee:ff", "nope"])
