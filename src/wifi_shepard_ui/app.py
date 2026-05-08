"""FastAPI app factory for the wifi-shepard read-only sidecar.

Exposes three GET routes (`/`, `/devices`, `/devices/{mac}`) plus `/healthz`.
No write paths — see AC-6 in ADR-0002.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from wifi_shepard_ui import views
from wifi_shepard_ui.db import open_readonly

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

    # Snapshot the env var at construction time. Tests monkeypatch the env
    # and rebuild the app per-case, which matches a real container restart.
    expected_token = os.environ.get("WIFI_SHEPARD_UI_TOKEN") or None

    def _safe_read(fn, default):
        """Run fn(conn) on a fresh read-only connection; return `default` if
        the database file is absent (AC-8: fresh deploy with no daemon yet)."""
        try:
            conn = _connect(db_path)
        except sqlite3.OperationalError:
            return default
        try:
            return fn(conn)
        finally:
            conn.close()

    @app.middleware("http")
    async def bearer_token_auth(request: Request, call_next):
        # /healthz must stay unauthenticated so docker healthcheck works
        # even when the token is set.
        if expected_token and request.url.path != "/healthz":
            header = request.headers.get("Authorization", "")
            scheme, _, presented = header.partition(" ")
            if (
                scheme.lower() != "bearer"
                or not presented
                or not secrets.compare_digest(presented, expected_token)
            ):
                return Response(status_code=401, content="unauthorized\n")
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
        rows = _safe_read(lambda c: views.list_devices(c, allowlist=set(), now=time.time()), [])
        rows = views.sort_devices(rows, sort)
        return templates.TemplateResponse(request, "devices.html", {"rows": rows, "sort": sort})

    @app.get("/devices/{mac}", response_class=HTMLResponse)
    def device_history(request: Request, mac: str):
        events = _safe_read(lambda c: views.device_history(c, mac=mac), [])
        return templates.TemplateResponse(request, "history.html", {"mac": mac, "events": events})

    _assert_no_write_routes(app)
    return app


app = create_app(db_path=Path(os.environ.get("WIFI_SHEPARD_DB_PATH", "/data/state.db")))
