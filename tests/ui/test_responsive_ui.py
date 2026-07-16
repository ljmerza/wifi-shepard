"""The sidecar renders as a responsive, themed document on every page.

The daemon's operator reaches this UI from a phone as often as a desktop, so
these are contract tests for the three things that actually break mobile:
a missing viewport tag (the page renders at ~980px and scales down), tables
with no per-cell labels to collapse into cards, and a hardcoded light theme.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wifi_shepard_ui.app import create_app

MAC_A = "AA:BB:CC:DD:EE:FF"

# Every user-reachable GET route. /healthz is plain text, not a page.
PAGE_PATHS = ["/", "/devices", f"/devices/{MAC_A}"]


@pytest.fixture
def client(seeded_db: Path) -> TestClient:
    return TestClient(create_app(db_path=seeded_db))


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_every_page_declares_a_viewport(client: TestClient, path: str) -> None:
    """Without this tag a mobile browser lays the page out at desktop width and
    zooms out — the single highest-impact responsiveness bug."""
    response = client.get(path)
    assert response.status_code == 200
    viewport = '<meta name="viewport" content="width=device-width, initial-scale=1">'
    assert viewport in response.text, f"{path} must declare a device-width viewport"


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_every_page_supports_dark_and_light(client: TestClient, path: str) -> None:
    response = client.get(path)
    text = response.text
    assert "prefers-color-scheme: dark" in text, f"{path} must define a dark palette"
    assert "color-scheme: light dark" in text, (
        f"{path} must set color-scheme so form controls/scrollbars follow the theme"
    )


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_every_page_collapses_tables_on_narrow_screens(client: TestClient, path: str) -> None:
    """The card-collapse rule and the data-label hook are load-bearing together:
    the CSS reveals labels via `content: attr(data-label)`, so a table whose
    cells lack the attribute silently loses its headers under 40rem."""
    text = client.get(path).text
    assert "max-width: 40rem" in text, f"{path} must define a narrow-screen breakpoint"
    assert "content: attr(data-label)" in text, f"{path} must surface labels when collapsed"


@pytest.mark.parametrize("path", ["/", "/devices", f"/devices/{MAC_A}"])
def test_data_cells_are_labelled_for_collapse(client: TestClient, path: str) -> None:
    """Each rendered body cell carries the label its collapsed card shows."""
    text = client.get(path).text
    assert 'data-label="' in text, f"{path} table cells must carry data-label for card collapse"


def test_devices_keeps_a_sort_affordance_when_headers_are_hidden(client: TestClient) -> None:
    """The narrow-screen rule hides <thead>, which is where the sort links live.
    Without a separate bar, sorting silently disappears on a phone — so the bar
    must offer the same keys as the header links.
    """
    text = client.get("/devices").text
    assert 'class="sort-bar"' in text, "narrow screens need a sort control outside <thead>"
    for key in ("name", "mac", "kicks", "last_bad", "state"):
        assert f'href="?sort={key}"' in text, f"sort bar must offer ?sort={key}"


def test_devices_sort_bar_marks_the_active_key(client: TestClient) -> None:
    text = client.get("/devices?sort=kicks").text
    assert '<a href="?sort=kicks" class="on">Kicks</a>' in text, (
        "the active sort key must be visually distinguished in the sort bar"
    )


def test_nav_marks_the_current_page(client: TestClient) -> None:
    assert '<a href="/" class="active">' in client.get("/").text
    assert '<a href="/devices" class="active">' in client.get("/devices").text


def test_device_history_keeps_devices_nav_active(client: TestClient) -> None:
    """A drill-down is still "Devices" — the nav shouldn't go blank there."""
    assert '<a href="/devices" class="active">' in client.get(f"/devices/{MAC_A}").text


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_pages_do_not_reference_external_assets(client: TestClient, path: str) -> None:
    """CSS stays inline on purpose: the bearer-token middleware guards every
    path except /healthz, and browsers don't send Authorization headers for
    subresources — an external stylesheet would 401 whenever a token is set."""
    text = client.get(path).text
    assert "<link" not in text, f"{path} must not pull an external stylesheet"
    assert "//cdn" not in text and "https://" not in text, f"{path} must not reference a CDN"
