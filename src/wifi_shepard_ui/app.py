"""FastAPI app factory for the wifi-shepard read-only sidecar.

Exposes three GET routes (`/`, `/devices`, `/devices/{mac}`) plus `/healthz`.
No write paths — see AC-6 in ADR-0002.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from wifi_shepard_ui import views
from wifi_shepard_ui.db import open_readonly

logger = logging.getLogger(__name__)


def _format_ts(ts: float | int | None) -> str:
    """Render an epoch ts as `YYYY-MM-DD HH:MM:SS UTC`. Returns '—' for None."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


# OperationalError messages we treat as "DB not yet populated" — render the
# empty-state page (AC-8) instead of a 500. Anything else (locked DB, disk
# I/O, corruption) is a real failure: log + re-raise so it surfaces.
_EMPTY_STATE_OPERATIONAL_ERRORS = (
    "unable to open database",  # file missing / dir not readable
    "no such table",  # daemon mid-startup, schema not yet created
)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# AC-6: v1 sidecar is read-only. The test file's grep catches write
# decorators at source-scan time; this set is the runtime fence checked
# inside create_app() after every route is registered.
_FORBIDDEN_HTTP_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def _assert_no_write_routes(app: FastAPI) -> None:
    offenders: list[str] = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        bad = _FORBIDDEN_HTTP_METHODS & {m.upper() for m in methods}
        if bad:
            offenders.append(f"{getattr(route, 'path', route)!r} -> {sorted(bad)}")
    if offenders:
        raise RuntimeError(
            "v1 wifi-shepard-ui must be read-only — found write routes: " + "; ".join(offenders)
        )


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the daemon's SQLite file in strict read-only mode (AC-5)."""
    return open_readonly(db_path)


def create_app(*, db_path: Path) -> FastAPI:
    app = FastAPI(title="wifi-shepard-ui", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["fmt_ts"] = _format_ts

    # Snapshot the env var at construction time. Tests monkeypatch the env
    # and rebuild the app per-case, which matches a real container restart.
    # Refuse an empty-string token: it disables auth silently and is almost
    # always an operator typo (cleared the value in wifi-shepard-ui.env).
    raw_token = os.environ.get("WIFI_SHEPARD_UI_TOKEN")
    if raw_token == "":
        raise RuntimeError(
            "WIFI_SHEPARD_UI_TOKEN is set to an empty string — refusing to start. "
            "Either unset the variable (no auth) or set a non-empty token."
        )
    expected_token = raw_token

    # AC-2: surface the allowlist flag per device. The daemon reads its own
    # allowlist from /config/config.yaml; the UI sidecar gets a parallel
    # WIFI_SHEPARD_UI_ALLOWLIST env (comma-separated MACs) so it doesn't
    # need to import from wifi_shepard.config.
    allowlist_raw = os.environ.get("WIFI_SHEPARD_UI_ALLOWLIST", "")
    allowlist = {m.strip() for m in allowlist_raw.split(",") if m.strip()}

    def _safe_read(fn, default):
        """Run fn(conn) on a fresh read-only connection; return `default` for
        the empty-state cases (AC-8: DB file absent; daemon mid-startup,
        schema not yet created). Any other OperationalError (locked DB,
        disk I/O, corruption) is logged and re-raised so it surfaces as a
        500, not a silently empty page."""
        conn: sqlite3.Connection | None = None
        try:
            try:
                conn = _connect(db_path)
            except sqlite3.OperationalError as e:
                if any(marker in str(e).lower() for marker in _EMPTY_STATE_OPERATIONAL_ERRORS):
                    return default
                logger.exception("wifi-shepard-ui: failed to open %s", db_path)
                raise
            try:
                return fn(conn)
            except sqlite3.OperationalError as e:
                if any(marker in str(e).lower() for marker in _EMPTY_STATE_OPERATIONAL_ERRORS):
                    return default
                logger.exception("wifi-shepard-ui: query against %s failed", db_path)
                raise
        finally:
            if conn is not None:
                conn.close()

    @app.middleware("http")
    async def bearer_token_auth(request: Request, call_next):
        # /healthz must stay unauthenticated so docker healthcheck works
        # even when the token is set.
        if expected_token and request.url.path != "/healthz":
            header = request.headers.get("Authorization", "")
            parts = header.split(None, 1)
            scheme = parts[0] if parts else ""
            presented = parts[1] if len(parts) > 1 else ""
            if (
                scheme.lower() != "bearer"
                or not presented
                or not secrets.compare_digest(presented, expected_token)
            ):
                return PlainTextResponse("unauthorized\n", status_code=401)
        return await call_next(request)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok\n"

    empty_stats = views.OverviewStats(
        total_clients=0, quarantined=0, kicks_today=0, kicks_this_week=0, noisy_aps=[]
    )

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        stats = _safe_read(lambda c: views.overview(c, now=time.time()), empty_stats)
        return templates.TemplateResponse(request, "overview.html", {"stats": stats})

    @app.get("/devices", response_class=HTMLResponse)
    def devices(request: Request, sort: str = "mac"):
        rows = _safe_read(lambda c: views.list_devices(c, allowlist=allowlist, now=time.time()), [])
        rows = views.sort_devices(rows, sort)
        return templates.TemplateResponse(request, "devices.html", {"rows": rows, "sort": sort})

    @app.get("/devices/{mac}", response_class=HTMLResponse)
    def device_history(request: Request, mac: str):
        events = _safe_read(lambda c: views.device_history(c, mac=mac), [])
        return templates.TemplateResponse(request, "history.html", {"mac": mac, "events": events})

    _assert_no_write_routes(app)
    return app


app = create_app(db_path=Path(os.environ.get("WIFI_SHEPARD_DB_PATH", "/data/state.db")))
