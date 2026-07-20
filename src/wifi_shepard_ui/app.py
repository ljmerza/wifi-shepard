"""FastAPI app factory for the wifi-shepard sidecar.

Read-only GET routes (`/`, `/devices`, `/devices/{mac}`, `/dns`, `/healthz`) plus the
ADR-0013 settings editor: `GET /settings` (form pre-filled from config.yaml) and the
one write path `POST /settings` (validate + round-trip write). Every other route stays
GET-only, enforced by `_assert_no_write_routes` (ADR-0002's blanket no-write rule,
amended by ADR-0013 to a single-path allowlist).
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from wifi_shepard_ui import config_io, device_config, settings_schema, views
from wifi_shepard_ui.db import MySQLReadConnection, open_readonly_any

logger = logging.getLogger(__name__)


def _format_ts(ts: float | int | None) -> str:
    """Render an epoch ts as `YYYY-MM-DD HH:MM:SS UTC`. Returns '—' for None."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


# UniFi radio identifiers → human band label for the noisy-APs table. Unknown
# ids pass through unchanged so a new band shows *something* rather than "?".
_RADIO_BANDS = {"ng": "2.4", "na": "5", "6e": "6"}


def _radio_band(radio: str | None) -> str:
    if not radio:
        return "?"
    return _RADIO_BANDS.get(radio, radio)


# Sparklines render into a fixed 0..100 viewBox and are stretched to the
# container by preserveAspectRatio="none" — so these are viewBox units, not
# pixels. The stroke stays crisp under that non-uniform scale via
# vector-effect="non-scaling-stroke" in the template.
_SPARK_W = 100.0
_SPARK_H = 100.0
_SPARK_PAD = 6.0  # vertical inset so peak/flat lines aren't clipped by the stroke


def _spark_points(values: Sequence[float] | None) -> str:
    """Map a numeric series to an SVG polyline `points` string.

    Returns "" for a series too short to draw (0 or 1 points); templates treat
    that as the no-data case. A flat series renders as a baseline rather than
    dividing by a zero range.
    """
    vals = [float(v) for v in (values or [])]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = _SPARK_W / (len(vals) - 1)
    usable = _SPARK_H - 2 * _SPARK_PAD
    return " ".join(
        f"{i * step:.2f},{_SPARK_H - _SPARK_PAD - ((v - lo) / span) * usable:.2f}"
        for i, v in enumerate(vals)
    )


def _spark_area(values: Sequence[float] | None) -> str:
    """Same series as _spark_points, closed down to the baseline for a fill."""
    points = _spark_points(values)
    if not points:
        return ""
    return f"0,{_SPARK_H:.0f} {points} {_SPARK_W:.0f},{_SPARK_H:.0f}"


def _merge_query(params: Mapping[str, str], **overrides: str) -> str:
    """Build a same-page href keeping the current /devices params, with overrides.

    Empty values are dropped, so overriding a filter to "" clears it from the
    URL. When nothing survives, a bare "?" still links back to the unfiltered
    page. Exposed to templates as the `qs` global.
    """
    merged = {**params, **overrides}
    encoded = urlencode([(k, v) for k, v in merged.items() if v])
    return f"?{encoded}" if encoded else "?"


def _refresh_seconds() -> int:
    """Overview auto-refresh interval (seconds) from env; default 60, 0 disables.

    A non-integer or negative value falls back to the 60s default rather than
    breaking the page with a bad <meta refresh> value.
    """
    raw = os.environ.get("WIFI_SHEPARD_UI_REFRESH_SECONDS", "60")
    try:
        seconds = int(raw)
    except ValueError:
        return 60
    return seconds if seconds >= 0 else 60


# OperationalError messages we treat as "DB not yet populated" — render the
# empty-state page (AC-8) instead of a 500. Anything else (locked DB, disk
# I/O, corruption) is a real failure: log + re-raise so it surfaces.
_EMPTY_STATE_OPERATIONAL_ERRORS = (
    "unable to open database",  # file missing / dir not readable
    "no such table",  # daemon mid-startup, schema not yet created
)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# The sidecar is read-only EXCEPT for the ADR-0013 settings save route. This runtime
# fence (checked in create_app after every route is registered) keeps every OTHER
# route GET-only, so a stray write endpoint still fails loudly. ADR-0002's original
# blanket no-write rule is amended by ADR-0013 to this single-path allowlist.
_FORBIDDEN_HTTP_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
_ALLOWED_WRITE_PATHS = frozenset({"/settings", "/devices/{mac}/settings"})


def _assert_no_write_routes(app: FastAPI) -> None:
    offenders: list[str] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if path in _ALLOWED_WRITE_PATHS:
            continue
        methods = getattr(route, "methods", None) or set()
        bad = _FORBIDDEN_HTTP_METHODS & {m.upper() for m in methods}
        if bad:
            offenders.append(f"{path!r} -> {sorted(bad)}")
    if offenders:
        raise RuntimeError(
            "wifi-shepard-ui must be read-only outside the settings save route — found "
            "unexpected write routes: " + "; ".join(offenders)
        )


# ADR-0014 device card: a heading per per-MAC object list. Only layout copy lives
# here — every field's label, unit, and description comes from the schema.
_DEVICE_GROUP_HEADINGS = {
    "overrides": "Tuning for this device",
    "reboot_override": "How to power-cycle this device",
}


def _safe_config(fn, default):
    """Run a config-file read, degrading to `default` if config.yaml is missing or
    malformed — a broken config must never 500 a read-only page."""
    try:
        return fn()
    except Exception:
        logger.exception("device settings: failed to read the config file")
        return default


def _device_memberships() -> list[tuple[settings_schema.MembershipSpec, settings_schema.FieldSpec]]:
    """(membership, its FieldSpec) pairs — the card reads the description off the spec."""
    pairs = []
    for membership in settings_schema.PER_DEVICE_MEMBERSHIPS:
        field = settings_schema.field_by_path(membership.path)
        if field is not None:
            pairs.append((membership, field))
    return pairs


def _device_groups(device_settings: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The per-MAC object-list groups, each with its editable leaves and current values.
    `mac` is skipped — the device's identity is the URL, not an input."""
    groups: list[dict[str, Any]] = []
    for key, prefix in settings_schema.PER_DEVICE_OBJECT_LISTS:
        fields = [
            (f, f.path[len(prefix) :])
            for f in settings_schema.item_fields(prefix)
            if f.path != f"{prefix}mac"
        ]
        groups.append(
            {
                "key": key,
                "heading": _DEVICE_GROUP_HEADINGS.get(key, key),
                "fields": fields,
                # NOT "values" — Jinja would resolve `group.values` to dict.values().
                "current": device_settings.get(key) or {},
            }
        )
    return groups


def _connect(db_path: Path, db_url: str | None = None) -> sqlite3.Connection | MySQLReadConnection:
    """Open the daemon's DB in strict read-only mode (AC-5): SQLite file, or
    the MySQL/MariaDB backend when WIFI_SHEPARD_DB_URL is set."""
    return open_readonly_any(db_path, db_url)


def _check_db_schema(db_path: Path, db_url: str | None = None) -> None:
    """Startup smoke-test: if the DB exists, fail fast on schema drift.

    Empty-state (DB file absent / DB server unreachable) is fine — the routes'
    `_safe_read` path handles that. The check exists to surface ADR-0003's
    coordinated-bump risk loudly at container startup, not per-request.
    """
    try:
        conn = open_readonly_any(db_path, db_url)
    except sqlite3.OperationalError:
        # File missing / dir not readable / DB server down — empty-state path
        # will handle it.
        return
    try:
        try:
            views.assert_kick_events_schema(conn)
        except sqlite3.OperationalError as e:
            # No-such-table = daemon mid-startup; empty-state path will handle it.
            if "no such table" in str(e).lower():
                return
            raise
    finally:
        conn.close()


def create_app(
    *, db_path: Path, config_path: Path | None = None, db_url: str | None = None
) -> FastAPI:
    _check_db_schema(db_path, db_url)
    # ADR-0013: the settings UI reads/writes this file. Default matches the daemon's
    # WIFI_SHEPARD_CONFIG (/config/config.yaml); the containing dir is bind-mounted
    # :rw into the sidecar (Phase 5 compose) so writes reach the daemon's file.
    if config_path is None:
        config_path = Path(os.environ.get("WIFI_SHEPARD_CONFIG_PATH", "/config/config.yaml"))
    app = FastAPI(title="wifi-shepard-ui", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["fmt_ts"] = _format_ts
    templates.env.filters["radio_band"] = _radio_band
    templates.env.filters["spark_points"] = _spark_points
    templates.env.filters["spark_area"] = _spark_area
    templates.env.globals["qs"] = _merge_query

    # Snapshot the auto-refresh interval at construction time (matches the env
    # snapshot pattern below; a container restart picks up changes).
    refresh_seconds = _refresh_seconds()

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

    # Surface the allowlist flag per device. ADR-0013: now that the sidecar reads
    # config.yaml, it reads the authoritative allowlist from there per request (so an
    # edit shows up without a container restart) — replacing the old parallel
    # allowlist env and its two-places-in-sync hazard.
    def _safe_read(fn, default):
        """Run fn(conn) on a fresh read-only connection; return `default` for
        the empty-state cases (AC-8: DB file absent; daemon mid-startup,
        schema not yet created). Any other OperationalError (locked DB,
        disk I/O, corruption) is logged and re-raised so it surfaces as a
        500, not a silently empty page."""
        conn: sqlite3.Connection | MySQLReadConnection | None = None
        try:
            try:
                conn = _connect(db_path, db_url)
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
        stats, dns_health = _safe_read(
            lambda c: (views.overview(c, now=time.time()), views.dns_source_health(c)),
            (empty_stats, []),
        )
        return templates.TemplateResponse(
            request,
            "overview.html",
            {
                "stats": stats,
                "dns_health": dns_health,
                "refresh_seconds": refresh_seconds,
                "active_page": "overview",
            },
        )

    @app.get("/devices", response_class=HTMLResponse)
    def devices(
        request: Request,
        sort: str = "mac",
        state: str = "",
        kicked_within: str = "",
        allowlist: str = "",
        q: str = "",
    ):
        now = time.time()
        allowed_macs = config_io.read_allowlist(config_path)
        all_rows = _safe_read(lambda c: views.list_devices(c, allowlist=allowed_macs, now=now), [])
        rows = views.filter_devices(
            all_rows, now=now, state=state, kicked_within=kicked_within, allowlist=allowlist, q=q
        )
        rows = views.sort_devices(rows, sort)
        # Current params for chip/sort hrefs (the qs global drops empties; the
        # default sort stays out of filter URLs so they read clean).
        params = {
            "sort": sort if sort != "mac" else "",
            "state": state,
            "kicked_within": kicked_within,
            "allowlist": allowlist,
            "q": q,
        }
        return templates.TemplateResponse(
            request,
            "devices.html",
            {
                "rows": rows,
                "total": len(all_rows),
                "sort": sort,
                "state": state,
                "kicked_within": kicked_within,
                "allowlist": allowlist,
                "q": q,
                "params": params,
                "filtered": any((state, kicked_within, allowlist, q)),
                "active_page": "devices",
                "auth_required": bool(expected_token),
            },
        )

    @app.get("/devices/{mac}", response_class=HTMLResponse)
    def device_history(request: Request, mac: str):
        now = time.time()
        allowed_macs = config_io.read_allowlist(config_path)
        events, summary = _safe_read(
            lambda c: (
                views.device_history(c, mac=mac),
                views.device_summary(c, mac=mac, allowlist=allowed_macs, now=now),
            ),
            ([], None),
        )
        # ADR-0014: the per-device card. Built from the schema so a future per-MAC
        # field renders here without a template edit. A missing/unreadable config
        # degrades to an all-off card rather than 500-ing the history page.
        device_settings = _safe_config(
            lambda: device_config.read_device_settings(config_path, mac),
            {},
        )
        return templates.TemplateResponse(
            request,
            "history.html",
            {
                "mac": mac,
                "events": events,
                "summary": summary,
                "active_page": "devices",
                "device_settings": device_settings,
                "memberships": _device_memberships(),
                "device_groups": _device_groups(device_settings),
                "auth_required": bool(expected_token),
            },
        )

    @app.post("/devices/{mac}/settings")
    async def device_settings_save(request: Request, mac: str):
        # JSON-only + header-carried bearer token, the same CSRF-safe posture as the
        # settings save route (ADR-0013 AC-9, ADR-0014 AC-6).
        #
        # The MAC is checked FIRST, before the body is read or the config is touched,
        # so a malformed path can never reach the filesystem (AC-7).
        if not device_config.is_valid_mac(mac):
            return JSONResponse(
                {"ok": False, "error": f"'{mac}' is not a valid MAC address"}, status_code=400
            )
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "expected a JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "expected a JSON object"}, status_code=400)
        try:
            device_config.apply_device_settings(config_path, mac, payload)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except OSError as exc:
            logger.exception("device settings: failed to write %s", config_path)
            return JSONResponse(
                {"ok": False, "error": f"could not write config: {exc}"}, status_code=500
            )
        return JSONResponse({"ok": True, "message": "Saved. Applies on the daemon's next scan."})

    @app.get("/dns", response_class=HTMLResponse)
    def dns(request: Request):
        # One connection for all three reads (ADR-0012). Each read tolerates its
        # table/column being absent (old daemon DB) by returning [].
        def _read(c):
            return (
                views.dns_source_health(c),
                views.dns_near_threshold(c),
                views.dns_thrash_kicks(c),
            )

        health, near, kicks = _safe_read(_read, ([], [], []))
        return templates.TemplateResponse(
            request,
            "dns.html",
            {"health": health, "near": near, "kicks": kicks, "active_page": "dns"},
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        # Read the live config.yaml to pre-fill the form (AC-2). A missing file yields
        # an all-defaults model (AC-10); a malformed file shows an error banner rather
        # than a 500 (still renders, so the operator can see what's wrong).
        read_error = None
        try:
            model = config_io.read_form_model(config_path)
        except Exception as exc:
            logger.exception("settings: failed to read %s", config_path)
            # A complete empty model so the page still renders (with the error banner
            # and all-default fields) instead of 500-ing on a malformed config.
            model = {
                "scalars": {},
                "scalar_lists": {},
                "object_lists": {},
                "section_enabled": {},
                "config_exists": False,
            }
            read_error = str(exc)
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "schema": settings_schema,
                "sections": settings_schema.SECTIONS,
                "model": model,
                "read_error": read_error,
                "auth_required": bool(expected_token),
                "active_page": "settings",
            },
        )

    @app.post("/settings")
    async def settings_save(request: Request):
        # JSON-only. A cross-site POST of application/json triggers a CORS preflight
        # this app never answers, so it can't be forged; with the header-carried bearer
        # token (auth middleware above) this write path is CSRF-safe (ADR-0013 AC-9).
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "expected a JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "expected a JSON object"}, status_code=400)
        try:
            mapping = config_io.build_mapping(payload)
            config_io.validate_mapping(mapping)  # daemon's own fail-closed validation (AC-4)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        try:
            config_io.write_config(config_path, mapping)  # round-trip, atomic (AC-5)
        except OSError as exc:
            logger.exception("settings: failed to write %s", config_path)
            return JSONResponse(
                {"ok": False, "error": f"could not write config: {exc}"}, status_code=500
            )
        return JSONResponse(
            {
                "ok": True,
                "message": (
                    "Saved. Threshold / scanner / backoff / quiet-hours / override changes apply "
                    "on the daemon's next scan. Connection, Home Assistant, DNS-source and "
                    "reboot on/off changes (marked “restart”) take effect after a daemon "
                    "restart."
                ),
            }
        )

    _assert_no_write_routes(app)
    return app


app = create_app(
    db_path=Path(os.environ.get("WIFI_SHEPARD_DB_PATH", "/data/state.db")),
    config_path=Path(os.environ.get("WIFI_SHEPARD_CONFIG_PATH", "/config/config.yaml")),
    # Same env var the daemon reads: set → both sides talk to MySQL/MariaDB,
    # unset → both sides use the SQLite file. One switch, no split-brain.
    db_url=os.environ.get("WIFI_SHEPARD_DB_URL") or None,
)
