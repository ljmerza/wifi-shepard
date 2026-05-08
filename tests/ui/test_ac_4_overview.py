"""AC-4: GET / overview shows total tracked clients, currently-quarantined
count, kicks-today, kicks-this-week, and top 5 noisy APs by cu_total."""

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


def test_ac_4_overview_null_ap_id_does_not_displace_real_aps(
    make_db: Callable[..., Path],
) -> None:
    """Regression for review #1: a client_samples row with ap_id=NULL must not
    consume a slot in the noisy_aps top-5. Seed 6 real APs + 1 NULL row; the
    page must surface 5 real APs (the lowest cu drops out, NULL never appears).
    """

    def _seed(conn: sqlite3.Connection, now: float) -> None:
        # Six real APs with descending cu_total, plus one NULL ap_id row whose
        # cu_total (95) is higher than every real AP. Without the SQL fix,
        # NULL would top the LIMIT 5 and one real AP would silently fall off.
        for i, cu in enumerate([90, 80, 70, 60, 50, 40]):
            conn.execute(
                "INSERT INTO client_samples "
                "(ts, mac, signal, tx_rate_kbps, tx_retries, "
                " wifi_tx_attempts, radio, ap_id, ap_cu_total) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now - i, f"AA:BB:CC:DD:EE:{i:02X}", -70, 6000, 0, 100, "ng", f"ap{i}", cu),
            )
        conn.execute(
            "INSERT INTO client_samples "
            "(ts, mac, signal, tx_rate_kbps, tx_retries, "
            " wifi_tx_attempts, radio, ap_id, ap_cu_total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, "FF:FF:FF:FF:FF:FF", -70, 6000, 0, 100, "ng", None, 95),
        )

    db_path = make_db(_seed)

    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import create_app

    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    text = response.text
    # All five top APs (ap0..ap4 by cu_total 90,80,70,60,50) must appear.
    for ap in ("ap0", "ap1", "ap2", "ap3", "ap4"):
        assert ap in text, f"top-5 noisy_aps must include {ap}; NULL ap_id should not displace it"
    # Sanity: the NULL row's marker cu_total of 95 must NOT render in the
    # noisy AP table (it's the giveaway that NULL took a slot).
    # We check for its tile cell shape ">95<" specifically; "95" can legitimately
    # appear elsewhere on the page in unrelated contexts.
    assert ">95<" not in text, "NULL-ap_id row must not appear in noisy AP table"
