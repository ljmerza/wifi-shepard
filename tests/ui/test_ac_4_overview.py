"""AC-4: GET / overview shows total tracked clients, currently-quarantined
count, kicks-today, kicks-this-week, and a top-5 noisy-APs table (AP name, MAC,
CPU, memory, per-channel utilization) sourced from the ap_samples tables."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from pathlib import Path


def test_ac_4_overview_renders_required_counts(seeded_db: Path) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=seeded_db)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    text = response.text
    lower = text.lower()

    # Required tile labels surfaced
    assert "tracked" in lower or "clients" in lower
    assert "quarantin" in lower
    assert "today" in lower
    assert "week" in lower
    assert any(marker in lower for marker in ["ap", "access point", "saturation"])
    assert "cu" in lower or "saturation" in lower or "channel" in lower

    # Specific seeded values — proves the implementation actually computed
    # the counts rather than templating constants.
    # Seeded fixture: 2 distinct MACs in client_samples; 0 quarantined;
    # 1 real kick (kicks_today should be 1, dry-run excluded);
    # ap_samples: "Front Porch" (peak cu 72) and "Garage" (peak cu 40).
    tile_values = re.findall(r'<div class="value">(\d+)</div>', text)
    assert len(tile_values) == 4, (
        f"expected 4 stat tiles (tracked/quarantine/today/week); got {tile_values}"
    )
    total, quarantine, today, week = tile_values
    assert total == "2", f"total tracked must be 2 distinct MACs, got {total}"
    assert quarantine == "0", f"quarantined must be 0, got {quarantine}"
    assert today == "1", f"kicks today must be 1 (dry-run excluded), got {today}"
    assert week == "1", f"kicks this week must be 1, got {week}"

    # Noisy AP table surfaces the seeded AP's name, MAC, CPU/mem load and a
    # per-channel utilization breakdown.
    assert "front porch" in lower, "noisy AP table must show the AP name"
    assert "ff:ee:dd:cc:bb:aa" in lower, "noisy AP table must show the AP MAC"
    assert "6%" in text, "Front Porch's CPU load (6%) must render"
    assert "42%" in text, "Front Porch's memory load (42%) must render"
    assert "ch6: 72%" in text, "per-channel utilization (2.4GHz ch6 at 72%) must render"

    # Ordering: ap1 (peak cu 72) must come before ap2 (peak cu 40).
    assert lower.find("front porch") < lower.find("garage"), (
        "noisy APs must be ordered by peak channel utilization, noisiest first"
    )


def test_ac_4_overview_top_5_aps_by_peak_cu(
    make_db: Callable[..., Path],
) -> None:
    """The noisy-APs table is capped at 5 and ordered by each AP's peak per-radio
    channel utilization. Seed 6 APs with descending peak cu; the 5 noisiest must
    surface (ordered) and the quietest must drop off.
    """

    def _seed(conn: sqlite3.Connection, now: float) -> None:
        # Six APs; each AP's single radio carries its peak cu. ap0 is noisiest.
        for i, cu in enumerate([90, 80, 70, 60, 50, 40]):
            conn.execute(
                "INSERT INTO ap_samples (ts, ap_id, name, mac, cpu_pct, mem_pct) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, f"ap{i}", f"AP-{i}", f"aa:bb:cc:dd:ee:{i:02x}", 5.0, 20.0),
            )
            conn.execute(
                "INSERT INTO ap_radio_samples (ts, ap_id, radio, channel, cu_total) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, f"ap{i}", "ng", 6, cu),
            )

    db_path = make_db(_seed)

    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    text = response.text
    lower = text.lower()
    # The five noisiest APs (AP-0..AP-4, peak cu 90..50) must appear, in order.
    positions = [lower.find(f"ap-{i}") for i in range(5)]
    assert all(p > 0 for p in positions), f"top-5 APs must all render; got {positions}"
    assert positions == sorted(positions), "APs must be ordered noisiest-first"
    # The quietest AP (AP-5, peak cu 40) must drop off the LIMIT 5.
    assert "ap-5" not in lower, "the 6th-noisiest AP must not appear in the top-5 table"
