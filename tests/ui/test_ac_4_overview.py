"""AC-4: GET / overview shows total tracked clients, currently-quarantined
count, kicks-today, kicks-this-week, and top 5 noisy APs by cu_total."""

from __future__ import annotations

import re
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
    # ap1 cu=70, ap2 cu=30 in noisy AP list.
    tile_values = re.findall(r'<div class="value">(\d+)</div>', text)
    assert len(tile_values) == 4, (
        f"expected 4 stat tiles (tracked/quarantine/today/week); got {tile_values}"
    )
    total, quarantine, today, week = tile_values
    assert total == "2", f"total tracked must be 2 distinct MACs, got {total}"
    assert quarantine == "0", f"quarantined must be 0, got {quarantine}"
    assert today == "1", f"kicks today must be 1 (dry-run excluded), got {today}"
    assert week == "1", f"kicks this week must be 1, got {week}"

    # Noisy AP row exists for ap1 with its seeded cu_total of 70.
    assert "ap1" in lower, "noisy AP table must include ap1"
    assert ">70<" in text, "ap1's seeded cu_total of 70 must render"
