"""GET /devices/{mac}?kind= filters the merged timeline to one event kind.

Server-side, URL-param driven, mirroring the /devices filter chips: "kick"
folds real + dry-run kicks together, "sample" keeps client_samples rows, and
an unknown value falls back to the full unfiltered timeline (never 500s).
"""

from __future__ import annotations

import ast
import inspect
import re
import time
from pathlib import Path

from wifi_shepard_ui import views
from wifi_shepard_ui.views import HistoryEvent

MAC_A = "AA:BB:CC:DD:EE:FF"  # seeded_db: 3 samples + 1 dry-run kick + 1 real kick


def _client(db_path: Path):
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    return TestClient(create_app(db_path=db_path))


# ---- filter_events unit level ---------------------------------------------


def _events() -> list[HistoryEvent]:
    now = time.time()
    return [
        HistoryEvent(ts=now - 10, kind="kick", detail="kick"),
        HistoryEvent(ts=now - 20, kind="kick_dry_run", detail="would-kick (dry-run)"),
        HistoryEvent(ts=now - 30, kind="sample", detail="signal=-72dBm"),
        HistoryEvent(ts=now - 40, kind="sample", detail="signal=-75dBm"),
    ]


def test_filter_events_kick_folds_real_and_dry_run() -> None:
    out = views.filter_events(_events(), kind="kick")
    assert [e.kind for e in out] == ["kick", "kick_dry_run"]


def test_filter_events_sample_keeps_only_samples() -> None:
    out = views.filter_events(_events(), kind="sample")
    assert [e.kind for e in out] == ["sample", "sample"]


def test_filter_events_empty_and_unknown_are_noops() -> None:
    events = _events()
    assert views.filter_events(events, kind="") == events
    # Hand-typed garbage must fall back to "no filter", never raise.
    assert views.filter_events(events, kind="banana") == events


def test_filter_events_is_case_insensitive_and_preserves_order() -> None:
    out = views.filter_events(_events(), kind="KICK")
    assert [e.kind for e in out] == ["kick", "kick_dry_run"]


def test_event_kinds_partitions_every_kind_the_read_model_emits() -> None:
    """EVENT_KINDS must claim every HistoryEvent.kind views.py can produce.

    A kind no chip claims stays visible under "All" but becomes silently
    unfilterable — the kind of gap that ships unnoticed. Reading the literals
    back out of the source keeps this honest: add a fourth kind and this fails
    until EVENT_KINDS names it.
    """
    calls = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(views)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "HistoryEvent"
    ]
    assert calls, "no HistoryEvent(...) construction found — did the read model move?"

    emitted = set()
    for call in calls:
        kw = next((k for k in call.keywords if k.arg == "kind"), None)
        assert kw is not None and isinstance(kw.value, ast.Constant), (
            f"HistoryEvent at views.py:{call.lineno} must pass kind= as a literal, "
            "otherwise this guard goes blind to it"
        )
        emitted.add(kw.value.value)

    claimed = set().union(*views.EVENT_KINDS.values())
    assert emitted == claimed

    # Claimed sets must not overlap, or All/Kicks/Samples stops being a
    # partition and a row would answer to two chips at once.
    assert sum(len(v) for v in views.EVENT_KINDS.values()) == len(claimed)


# ---- /devices/{mac} route level -------------------------------------------


def test_history_kind_kick_hides_samples(seeded_db: Path) -> None:
    with _client(seeded_db) as client:
        text = client.get(f"/devices/{MAC_A}?kind=kick").text
    lower = text.lower()
    assert "kick" in lower and "dry-run kick" in lower, "both kick rows must survive"
    # -72/-75 only ever appear in sample table rows (the Signal tile shows the
    # latest sample, -78), so their absence proves the sample rows are hidden.
    assert not any(s in text for s in ["-72", "-75"]), "sample rows must be hidden"
    assert "2 of 5 event" in text
    assert "Clear filter" in text


def test_history_kind_sample_hides_kicks(seeded_db: Path) -> None:
    with _client(seeded_db) as client:
        text = client.get(f"/devices/{MAC_A}?kind=sample").text
    assert "-72" in text, "sample rows must survive"
    assert "dry-run kick" not in text.lower(), "kick rows must be hidden"
    assert "3 of 5 event" in text


def test_history_default_and_chips_present(seeded_db: Path) -> None:
    with _client(seeded_db) as client:
        text = client.get(f"/devices/{MAC_A}").text
    # Unfiltered default shows every kind and no "of N" narrowing.
    assert "dry-run kick" in text.lower()
    assert "-72" in text
    assert " of 5 event" not in text
    # Filter chips are rendered and link back to the same page via ?kind=.
    assert "?kind=kick" in text
    assert "?kind=sample" in text


def test_history_unknown_kind_renders_full_timeline(seeded_db: Path) -> None:
    with _client(seeded_db) as client:
        response = client.get(f"/devices/{MAC_A}?kind=banana")
    assert response.status_code == 200
    text = response.text
    assert "dry-run kick" in text.lower() and "-72" in text
    assert " of 5 event" not in text, "unknown kind must not read as a filter"


def test_kicks_tile_links_to_the_kick_filtered_timeline(seeded_db: Path) -> None:
    """The Kicks tile is the discovery path into ?kind=kick, mirroring the way
    the overview tiles link into a pre-filtered /devices."""
    with _client(seeded_db) as client:
        text = client.get(f"/devices/{MAC_A}").text
        # The tile is an anchor wrapping the kick count, not a bare div.
        tile = re.search(r'<a class="tile[^"]*" href="\?kind=kick">(.*?)</a>', text, re.S)
        assert tile is not None, "Kicks tile must render as a link to ?kind=kick"
        assert "Kicks" in tile.group(1)
        # And the href it advertises really lands on the kick-filtered view.
        assert "2 of 5 event" in client.get(f"/devices/{MAC_A}?kind=kick").text


def test_history_kind_with_no_matches_shows_filter_empty_state(make_db) -> None:
    """A device that only has samples, filtered to kicks, keeps the tiles +
    filter bar and shows the filter-empty note, not the device-empty note."""

    def seed(conn, now: float) -> None:
        conn.execute(
            "INSERT INTO client_samples "
            "(ts, mac, signal, tx_rate_kbps, tx_retries, "
            " wifi_tx_attempts, radio, ap_id, ap_cu_total, name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now - 30, MAC_A, -60, 6000, 5, 100, "ng", "ap1", 40, "wled"),
        )

    db = make_db(seed)
    with _client(db) as client:
        text = client.get(f"/devices/{MAC_A}?kind=kick").text
    assert "No events match the current filter" in text
    assert "No events recorded" not in text, "device has history — not the empty-DB state"
    assert "?kind=sample" in text, "filter bar must still render"
