"""Overview sparklines: hourly kick trend + per-AP channel-utilization trend.

Two layers are tested separately: the bucketing/queries in views.py, and the
pure series->SVG geometry in app.py. The rendering tests then assert the two
meet in the template without disturbing the AC-4 stat tiles.
"""

from __future__ import annotations

import re
import sqlite3

from fastapi.testclient import TestClient

from wifi_shepard_ui.app import _spark_area, _spark_points, create_app
from wifi_shepard_ui.views import kicks_by_hour

MAC = "AA:BB:CC:DD:EE:FF"


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------


def test_spark_points_empty_series_renders_nothing() -> None:
    assert _spark_points([]) == ""
    assert _spark_points(None) == ""


def test_spark_points_single_point_renders_nothing() -> None:
    """One sample is not a trend — a 1-point polyline draws an invisible dot,
    so the template's no-data branch should win instead."""
    assert _spark_points([7]) == ""


def test_spark_points_maps_series_across_the_viewbox() -> None:
    """Min pins to the baseline, max to the top inset, x spans the full width."""
    assert _spark_points([0, 10]) == "0.00,94.00 100.00,6.00"


def test_spark_points_flat_series_does_not_divide_by_zero() -> None:
    """An all-zero day (no kicks) is the common case — it must render a flat
    baseline, not raise ZeroDivisionError."""
    assert _spark_points([0, 0, 0]) == "0.00,94.00 50.00,94.00 100.00,94.00"


def test_spark_points_scales_x_to_series_length() -> None:
    points = _spark_points([1, 2, 3, 4, 5])
    xs = [float(p.split(",")[0]) for p in points.split()]
    assert xs == [0.0, 25.0, 50.0, 75.0, 100.0]


def test_spark_area_closes_the_polygon_to_the_baseline() -> None:
    assert _spark_area([0, 10]) == "0,100 0.00,94.00 100.00,6.00 100,100"


def test_spark_area_empty_series_renders_nothing() -> None:
    assert _spark_area([]) == ""


# --------------------------------------------------------------------------
# kicks_by_hour bucketing
# --------------------------------------------------------------------------


def test_kicks_by_hour_buckets_only_real_kicks_in_window(make_db) -> None:
    """Seeder and assertion share one pinned `now`: make_db stamps its own
    time.time(), and that sub-millisecond skew is enough to floor a kick sitting
    exactly on an hour boundary into the previous bucket. Offsets are also kept
    mid-bucket so the expectations don't hinge on boundary rounding.
    """
    import time

    now = time.time()

    def seed(conn: sqlite3.Connection, _seed_now: float) -> None:
        # Two real kicks in the most recent hour (bucket 23).
        conn.execute("INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 0)", (now - 60, MAC))
        conn.execute(
            "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 0)", (now - 600, MAC)
        )
        # One real kick 5.5h ago -> bucket 18.
        conn.execute(
            "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 0)",
            (now - (5 * 3600 + 1800), MAC),
        )
        # A dry-run in the newest hour — must NOT count.
        conn.execute(
            "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 1)", (now - 120, MAC)
        )
        # A real kick 30h ago — outside the window, must NOT count.
        conn.execute(
            "INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 0)", (now - 30 * 3600, MAC)
        )

    conn = sqlite3.connect(make_db(seed))
    try:
        series = kicks_by_hour(conn, now=now, hours=24)
    finally:
        conn.close()

    assert len(series) == 24, "one bucket per trailing hour"
    assert series[23] == 2, f"two real kicks in the newest hour; got {series[23]}"
    assert series[18] == 1, f"one real kick 5.5 hours ago; got {series[18]}"
    assert sum(series) == 3, (
        f"dry-run and out-of-window kicks must be excluded; got total {sum(series)}"
    )


def test_kicks_by_hour_empty_table_is_all_zeros(make_db) -> None:
    import time

    conn = sqlite3.connect(make_db(None))
    try:
        series = kicks_by_hour(conn, now=time.time(), hours=24)
    finally:
        conn.close()
    assert series == tuple([0] * 24)


def test_kicks_by_hour_counts_a_kick_landing_exactly_now(make_db) -> None:
    """Clamp check: ts == now indexes one past the last bucket before clamping,
    which would silently drop the most recent kick."""
    import time

    now = time.time()

    def seed(conn: sqlite3.Connection, _seed_now: float) -> None:
        conn.execute("INSERT INTO kick_events (ts, mac, dry_run) VALUES (?, ?, 0)", (now, MAC))

    conn = sqlite3.connect(make_db(seed))
    try:
        series = kicks_by_hour(conn, now=now, hours=24)
    finally:
        conn.close()
    assert series[23] == 1, "a kick at exactly `now` belongs to the current hour"


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_overview_renders_kick_sparkline(seeded_db) -> None:
    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        text = client.get("/").text

    assert 'class="spark-line"' in text, "the kick trend must render a polyline"
    assert 'viewBox="0 0 100 100"' in text
    assert 'vector-effect="non-scaling-stroke"' in text, (
        "a stretched viewBox distorts stroke width without this"
    )


def test_overview_renders_ap_utilization_sparkline(seeded_db) -> None:
    """conftest seeds one ap_radio_samples row per radio, which is a 1-point
    series — too short to chart, so the AP trend cell shows the dash."""
    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        text = client.get("/").text
    assert 'data-label="Trend"' in text, "the noisy-AP table must expose a Trend column"


def test_ap_sparkline_appears_once_the_radio_has_history(make_db) -> None:
    def seed(conn: sqlite3.Connection, now: float) -> None:
        conn.execute(
            "INSERT INTO ap_samples (ts, ap_id, name, mac, cpu_pct, mem_pct) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, "ap1", "Front Porch", "ff:ee:dd:cc:bb:aa", 6.0, 42.0),
        )
        for i, cu in enumerate([10, 30, 55, 72]):
            conn.execute(
                "INSERT INTO ap_radio_samples (ts, ap_id, radio, channel, cu_total) "
                "VALUES (?, ?, ?, ?, ?)",
                (now - (4 - i) * 300, "ap1", "ng", 6, cu),
            )

    app = create_app(db_path=make_db(seed))
    with TestClient(app) as client:
        text = client.get("/").text

    assert text.count('class="spark-line"') >= 2, (
        "with radio history, both the kick trend and the AP trend must chart"
    )
    assert "Recent channel utilization trend for Front Porch" in text, (
        "the AP sparkline needs an accessible label"
    )


def test_sparklines_do_not_disturb_the_ac4_stat_tiles(seeded_db) -> None:
    """AC-4 pins exactly four `<div class="value">N</div>` tiles. Sparkline
    markup must not add a fifth or the overview contract regresses."""
    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        text = client.get("/").text
    assert len(re.findall(r'<div class="value">(\d+)</div>', text)) == 4


def test_overview_survives_a_db_without_ap_tables(tmp_path) -> None:
    """Pre-upgrade daemon DB: kick trend still charts, AP trend degrades to the
    empty state rather than 500-ing."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE client_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mac TEXT NOT NULL
        );
        CREATE TABLE kick_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mac TEXT NOT NULL,
            dry_run INTEGER NOT NULL DEFAULT 0,
            mechanism TEXT NOT NULL DEFAULT 'deauth',
            target_bssid TEXT, attempt_group TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "no ap data" in response.text.lower()
