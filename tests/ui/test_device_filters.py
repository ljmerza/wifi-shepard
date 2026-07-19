"""/devices URL-param filters (state, kicked_within, allowlist, q) and the
overview tiles that deep-link to them.

Filters AND together, ride the same query string as ?sort=, and unknown
values fall back to "no filter" instead of 500-ing (they're hand-editable
URL params).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from wifi_shepard_ui import views

MAC_KICKED = "aa:bb:cc:dd:ee:01"  # 1 real kick 90s ago -> KICKED (cooldown 300s)
MAC_NORMAL = "aa:bb:cc:dd:ee:02"  # samples only, never kicked -> NORMAL
MAC_QUAR = "aa:bb:cc:dd:ee:03"  # 5 real kicks, newest 10 days ago -> QUARANTINE


def _seed_states(conn: sqlite3.Connection, now: float) -> None:
    for mac, name in [
        (MAC_KICKED, "wled-kitchen"),
        (MAC_NORMAL, "thermostat"),
        (MAC_QUAR, None),
    ]:
        conn.execute(
            "INSERT INTO client_samples "
            "(ts, mac, signal, tx_rate_kbps, tx_retries, "
            " wifi_tx_attempts, radio, ap_id, ap_cu_total, name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now - 30, mac, -60, 6000, 5, 100, "ng", "ap1", 40, name),
        )
    conn.execute(
        "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 0)",
        (now - 90, MAC_KICKED),
    )
    for days_ago in (10, 11, 12, 13, 14):
        conn.execute(
            "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 0)",
            (now - days_ago * 86400, MAC_QUAR),
        )


def _client(db_path: Path, config_path: Path | None = None):
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    kwargs = {"db_path": db_path}
    if config_path is not None:
        kwargs["config_path"] = config_path
    return TestClient(create_app(**kwargs))


def _macs_shown(text: str) -> set[str]:
    return {mac for mac in (MAC_KICKED, MAC_NORMAL, MAC_QUAR) if mac in text.lower()}


# ---- filter_devices unit level -------------------------------------------


def _row(mac: str, **overrides) -> views.DeviceRow:
    defaults = dict(
        mac=mac,
        kick_count=0,
        last_kick_ts=None,
        last_event_ts=None,
        state="NORMAL",
        allowlisted=False,
        name=None,
    )
    defaults.update(overrides)
    return views.DeviceRow(**defaults)


def test_filter_devices_ands_all_filters_together() -> None:
    now = time.time()
    rows = [
        _row("aa", state="KICKED", kick_count=2, last_kick_ts=now - 100, name="wled-porch"),
        _row("bb", state="KICKED", kick_count=1, last_kick_ts=now - 100, name="plug"),
        _row("cc", state="NORMAL", name="wled-attic"),
    ]
    out = views.filter_devices(rows, now=now, state="kicked", kicked_within="24h", q="wled")
    assert [r.mac for r in out] == ["aa"]


def test_filter_devices_kicked_within_windows_and_never() -> None:
    now = time.time()
    recent = _row("aa", kick_count=1, last_kick_ts=now - 3600, state="EVALUATING")
    old = _row("bb", kick_count=1, last_kick_ts=now - 86400 * 10, state="NORMAL")
    never = _row("cc")
    rows = [recent, old, never]

    assert views.filter_devices(rows, now=now, kicked_within="24h") == [recent]
    assert views.filter_devices(rows, now=now, kicked_within="7d") == [recent]
    assert views.filter_devices(rows, now=now, kicked_within="30d") == [recent, old]
    assert views.filter_devices(rows, now=now, kicked_within="never") == [never]


def test_filter_devices_allowlist_and_unknown_values_are_noops() -> None:
    now = time.time()
    listed = _row("aa", allowlisted=True)
    unlisted = _row("bb")
    rows = [listed, unlisted]

    assert views.filter_devices(rows, now=now, allowlist="yes") == [listed]
    assert views.filter_devices(rows, now=now, allowlist="no") == [unlisted]
    # Hand-typed garbage must fall back to "no filter", never raise.
    unfiltered = views.filter_devices(
        rows, now=now, state="banana", kicked_within="fortnight", allowlist="maybe"
    )
    assert unfiltered == rows


# ---- /devices route level --------------------------------------------------


def test_devices_state_filter_via_url_param(make_db) -> None:
    db = make_db(_seed_states)
    with _client(db) as client:
        assert _macs_shown(client.get("/devices?state=kicked").text) == {MAC_KICKED}
        assert _macs_shown(client.get("/devices?state=normal").text) == {MAC_NORMAL}
        assert _macs_shown(client.get("/devices?state=quarantine").text) == {MAC_QUAR}


def test_devices_kicked_within_filter_via_url_param(make_db) -> None:
    db = make_db(_seed_states)
    with _client(db) as client:
        assert _macs_shown(client.get("/devices?kicked_within=24h").text) == {MAC_KICKED}
        assert _macs_shown(client.get("/devices?kicked_within=30d").text) == {
            MAC_KICKED,
            MAC_QUAR,
        }
        assert _macs_shown(client.get("/devices?kicked_within=never").text) == {MAC_NORMAL}


def test_devices_q_filter_matches_name_or_mac(make_db) -> None:
    db = make_db(_seed_states)
    with _client(db) as client:
        assert _macs_shown(client.get("/devices?q=wled").text) == {MAC_KICKED}
        assert _macs_shown(client.get("/devices?q=ee:03").text) == {MAC_QUAR}


def test_devices_allowlist_filter_reads_config(make_db, tmp_path: Path) -> None:
    db = make_db(_seed_states)
    config = tmp_path / "config.yaml"
    config.write_text(f'allowlist:\n  - "{MAC_NORMAL}"\n')
    with _client(db, config_path=config) as client:
        assert _macs_shown(client.get("/devices?allowlist=yes").text) == {MAC_NORMAL}
        assert _macs_shown(client.get("/devices?allowlist=no").text) == {
            MAC_KICKED,
            MAC_QUAR,
        }


def test_devices_filters_compose_with_sort_and_survive_in_chip_links(make_db) -> None:
    db = make_db(_seed_states)
    with _client(db) as client:
        text = client.get("/devices?sort=kicks&state=kicked").text
    assert _macs_shown(text) == {MAC_KICKED}
    # Chip hrefs must carry the active sort (& is Jinja-escaped in attributes).
    assert "sort=kicks&amp;state=kicked" in text or "sort=kicks&amp;kicked_within=" in text, (
        "filter chip links must preserve the active ?sort="
    )
    # Match count reflects the filter: 1 of 3 devices.
    assert "1 of 3 device" in text
    assert "Clear filters" in text


def test_devices_unknown_filter_values_render_unfiltered(make_db) -> None:
    db = make_db(_seed_states)
    with _client(db) as client:
        response = client.get("/devices?state=banana&kicked_within=fortnight")
    assert response.status_code == 200
    assert _macs_shown(response.text) == {MAC_KICKED, MAC_NORMAL, MAC_QUAR}


def test_devices_no_match_shows_filter_empty_state_not_db_empty_state(make_db) -> None:
    db = make_db(_seed_states)
    with _client(db) as client:
        text = client.get("/devices?q=zzz-no-such-device").text
    assert "No devices match the current filters" in text
    assert "Clear filters" in text
    assert "No clients tracked yet" not in text


def test_overview_tiles_link_to_prefiltered_devices(seeded_db: Path) -> None:
    with _client(seeded_db) as client:
        text = client.get("/").text
    assert 'href="/devices?state=quarantine"' in text
    assert 'href="/devices?kicked_within=24h"' in text
    assert 'href="/devices?kicked_within=7d"' in text
