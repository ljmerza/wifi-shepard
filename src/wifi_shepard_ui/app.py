"""FastAPI app factory for the wifi-shepard read-only sidecar.

Exposes three GET routes (`/`, `/devices`, `/devices/{mac}`) plus `/healthz`.
No write paths — see AC-6 in ADR-0002.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from wifi_shepard_ui import views

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the daemon's SQLite file. Read-only enforcement comes in AC-5."""
    return sqlite3.connect(db_path)


def create_app(*, db_path: Path) -> FastAPI:
    app = FastAPI(title="wifi-shepard-ui", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok\n"

    @app.get("/devices", response_class=HTMLResponse)
    def devices(request: Request, sort: str = "mac"):
        conn = _connect(db_path)
        try:
            rows = views.list_devices(conn, allowlist=set(), now=time.time())
        finally:
            conn.close()
        rows = views.sort_devices(rows, sort)
        return templates.TemplateResponse(request, "devices.html", {"rows": rows, "sort": sort})

    @app.get("/devices/{mac}", response_class=HTMLResponse)
    def device_history(request: Request, mac: str):
        conn = _connect(db_path)
        try:
            events = views.device_history(conn, mac=mac)
        finally:
            conn.close()
        return templates.TemplateResponse(request, "history.html", {"mac": mac, "events": events})

    return app


app = create_app(db_path=Path(os.environ.get("WIFI_SHEPARD_DB_PATH", "/data/state.db")))
