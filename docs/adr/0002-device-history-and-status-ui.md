# ADR-0002: UI for Device History & WiFi Status Overview

**Status:** Proposed
**Date:** 2026-05-08
**Author:** Leonardo Merza

## Context

### Background

[`ADR-0001`](./0001-mvp-scope-base-feature.md) commits the daemon to writing two SQLite tables on every poll cycle:

- `client_samples` — per-poll per-client snapshot (`signal`, `tx_rate_kbps`, `tx_retries`, `wifi_tx_attempts`, `radio`, `ap_id`, `ap_cu_total`, `ts`).
- `kick_events` — every action including `dry_run=true` "would-kick" rows (`mac`, `ts`, `dry_run`, reason fields).

[`PLAN.md`](../../PLAN.md) §12 names a *"FastAPI + HTMX read-only dashboard: current bad-state list, kick history, allowlist, quarantine"* under "way down the road" but does not pick an architecture. ADR-0001's *Anticipated follow-ups* lists *"observability — Prometheus `/metrics` endpoint vs OpenTelemetry traces vs HA MQTT discovery"*, framed around metrics; this ADR broadens that follow-up to also cover the **per-device history list** that doesn't fit a time-series store cleanly.

### Current State

- Only ADR is [`0001`](./0001-mvp-scope-base-feature.md) (Proposed). This is ADR-0002.
- Daemon has no HTTP server and no web-framework dependency. `pyproject.toml` carries `pydantic`, `structlog`, `aiosqlite`, `httpx`, `aiounifi` — nothing that serves HTTP.
- `/data/state.db` is mounted as a named volume in `docker-compose.fragment.yml`; the daemon is the only writer.
- The monorepo (`/media/cubxi/docker`) already runs Prometheus + Grafana (`docker-compose.monitoring.yml`) and Home Assistant + an MQTT-capable HA install (`docker-compose.home.yml`) — both are "free" infra to lean on.
- The daemon emits structured JSON logs to stdout; the monorepo does not yet run Loki, but adding it would be straightforward.

### Requirements

1. A **per-device history view**: for any tracked MAC, show the chronological timeline of bad-state windows, kick events (real and dry-run), and current backoff state (`NORMAL` / `KICK_PENDING` / `KICKED` / `EVALUATING` / `QUARANTINE`).
2. A **device list view**: every MAC the daemon has seen, with total kick count, last-bad-window timestamp, current state, and allowlist flag — sortable so noisy devices float to the top.
3. An **overview view**: count of currently bad-state clients, kicks today, kicks this week, currently-quarantined MACs, top-N noisy APs by `cu_total`. The first thing an operator sees on visiting the URL.
4. **Read-only in v1.** No allowlist edits, no force-unquarantine, no threshold tuning from the UI. Mutating operations stay config-driven (`/config/config.yaml` + SIGHUP).
5. **Failure-isolated from the daemon.** A bug in the UI must not stop the kicker.
6. Must run inside the existing `/media/cubxi/docker` monorepo via `docker-compose.local.yml` (graduating to `home.yml` once stable, like every other service).

### Constraints

- One Python process *for the daemon* — adding a web server inside that process is a real architectural choice, not free (ADR-0001 §Constraints).
- SQLite WAL: a second reader is fine, but it must open the file with `mode=ro` and respect the daemon's writer.
- No new outbound dependencies if the existing monitoring stack already does the job.
- No public exposure: this is a homelab tool. Auth is optional, defaulting to none.
- Schema discipline: the UI reads the daemon's tables. A schema change in the daemon must not silently break the UI — either co-locate the read model or version the schema.

## Options Considered

### Option 1: FastAPI + HTMX read-only sidecar (Chosen)

**Description:** A second container, `wifi-shepard-ui`, built from a separate `Dockerfile.ui`, mounts `/data:ro` and serves server-rendered HTML pages backed by SQLite queries. HTMX (drop-in `<script>` tag, no build step) provides progressive enhancement (live-refresh of the overview, in-place row expansion) but is not required for any read path.

**Pros:**

- Tabular, event-shaped data is the first-class model. `kick_events` joined with `mac_state` and the latest `client_samples` is exactly a SQL view; rendering it is a `SELECT` + a Jinja template.
- Failure-isolated by container boundary. UI OOM, lock contention bug, or accidental long-running query cannot stall the daemon's poll loop.
- Daemon `pyproject.toml` stays free of FastAPI/uvicorn. The kicker stays a process supervisor.
- Mounts SQLite read-only — `aiosqlite.connect("file:/data/state.db?mode=ro", uri=True)` — there is no path by which the UI can corrupt daemon state.
- Independent iteration cadence: UI releases don't redeploy the daemon, daemon releases don't redeploy the UI. Each has its own image tag.
- Additive: a Prometheus exporter (Option 3), a Grafana SQLite plugin board (Option 4), or HA MQTT discovery (Option 5) can layer on top later without re-architecture.
- Matches `PLAN.md` §12 verbatim ("FastAPI + HTMX read-only dashboard").

**Cons:**

- One more container in `docker-compose.local.yml`.
- Schema coupling: the UI knows table column names. Mitigated by isolating reads to a single `views.py` module (treated as the read-model contract), and by the daemon owning the schema in `db.py` migrations — schema changes require a coordinated bump.
- Reinvents some Grafana niceties (auth, share-by-URL, dashboard JSON export) at the cost of code we own.

### Option 2: FastAPI + HTMX in-process (same container as the daemon)

**Description:** Add FastAPI/uvicorn to the daemon's `pyproject.toml`. Run the HTTP server in the same asyncio event loop as the scanner; expose port 8080.

**Pros:**

- One container, one deploy. Slightly less docker-compose sprawl.
- HTTP handlers can read in-memory state directly (`mac_state` dict, current backoff state) without re-querying SQLite — small latency win.
- No "read your own writes" lag — the UI sees state the moment the scanner updates it.

**Cons:**

- A misbehaving handler (slow query, memory leak, client holding a long-lived connection) shares the daemon's event loop. The daemon's primary job — kicking — gains a co-tenant.
- Adds FastAPI/uvicorn/jinja2 to the daemon image, growing the attack surface and supply chain of a process that already needs network access to the WiFi controller.
- Couples release cadence: any UI change ships in a daemon redeploy.
- Testing surface grows in a process where the existing tests are hot paths (scorer, backoff, actor).

### Option 3: Prometheus exporter + Grafana

**Description:** Daemon exposes `/metrics` on a small HTTP handler (or via a sidecar that scrapes SQLite). Gauges: `wifi_shepard_clients_in_bad_state`, `wifi_shepard_kicks_total{mac=...}`, `wifi_shepard_ap_cu_total{ap=...}`, `wifi_shepard_quarantined_total`. Grafana panels in the existing monitoring stack render trends. This is the option ADR-0001 explicitly anticipated as a follow-up.

**Pros:**

- Zero new infrastructure: the monorepo's `docker-compose.monitoring.yml` already runs Prometheus and Grafana.
- Native time-series — kick rate over the last 7 days, AP saturation over the last hour, SLO-style alerts via Alertmanager become free.
- Industry-standard. The shape of `/metrics` is well-understood and stable across operators.

**Cons:**

- Prometheus is a **time-series store**, not an event store. Listing every `kick_event` for MAC `X` is exactly the wrong query. To get the per-device history view the user named as a primary goal, we'd still need either Loki or a separate UI on top of SQLite — meaning Option 3 alone doesn't satisfy Requirement 1.
- High-cardinality labels (`mac=AA:BB:CC:DD:EE:FF`) are a known Prometheus anti-pattern. A homelab with ~30 clients is fine; an IoT-heavy network creeping toward 100+ MACs flirts with the line.
- Still requires an HTTP server inside the daemon (or a scraper sidecar reading SQLite). Doesn't fully escape the "add a web server" cost of Option 2.

### Option 4: Grafana + SQLite datasource plugin

**Description:** Install the [community SQLite Grafana datasource plugin](https://grafana.com/grafana/plugins/frser-sqlite-datasource/), point it at `/data/state.db`, build dashboards on top with no daemon-side changes.

**Pros:**

- Zero changes to the daemon. Pure read-side innovation.
- Full event history available — the same SQL the chosen sidecar would write, but rendered in Grafana.
- Reuses Grafana's auth, share-by-URL, alerting, and dashboard JSON.

**Cons:**

- The SQLite datasource is community-maintained, not part of core Grafana. Plugin upgrades and Grafana upgrades can desync; the homelab takes on its maintenance.
- Grafana has to mount the daemon's volume read-only — coupling deployment in a way the existing Grafana doesn't currently do for any other service.
- Concurrent reader from another container with WAL mode is OK in principle but more failure modes than a same-host sidecar (mount semantics, file-system caching).
- Grafana's table panel is workable for "list every kick" but the UX is built for time-series first; per-MAC drill-downs require dashboard variables and feel awkward.

### Option 5: Home Assistant MQTT discovery

**Description:** Daemon publishes one MQTT discovery payload per tracked MAC (`sensor.wifi_<mac>_kicks`, `binary_sensor.wifi_<mac>_bad_state`, `sensor.wifi_<mac>_state`). HA auto-creates entities; the operator builds Lovelace cards.

**Pros:**

- Lives where the homelab's notifications already are. Phone access via the HA app for free.
- Notify on bad-state transitions becomes trivial — HA automation, not daemon code.
- No new web UI to maintain; reuses HA's existing dashboarding.

**Cons:**

- Adds an MQTT broker dependency (no broker is in the monorepo today; the HA REST notifier in ADR-0001 is HTTP, not MQTT). New stack file work.
- HA Lovelace YAML is brittle and hard to template per-MAC across ~30 devices.
- HA Recorder is fine for short-window state history but is not the right store for a free-form list of every `kick_event` ever recorded.
- Couples the daemon's data model to HA's entity model. Renaming a state value becomes a coordinated change.

### Option 6: Defer — CLI subcommand + Docker logs only

**Description:** No new UI surface. Add a `wifi-shepard report devices` and `wifi-shepard report mac AA:BB:...` subcommand that reads SQLite and prints tables. Operator uses `docker exec` plus `dca logs`. Optionally add Loki later for log-driven dashboards.

**Pros:**

- Zero new components. Smallest possible scope.
- Consistent with `PLAN.md` §5 ("no UI in v1").
- Easy to revisit once real kick patterns are observed in the wild — informs what views actually matter.

**Cons:**

- Doesn't answer the user's stated need. CLI tables aren't an "overview at a glance".
- Punts the architectural choice to a later ADR with no new information — research and trade-offs must be repeated.
- No URL to bookmark, no phone access, no shareable view.

## Decision

**Chosen Option:** Option 1 — FastAPI + HTMX read-only sidecar.

**Rationale:**

1. The data the user named (per-device kick history, reset counts, current backoff state) is **event-shaped and tabular**, not time-series. SQL queries against `kick_events` and `client_samples` are the natural model; Grafana panels (Options 3/4) would force the data into shapes that fit time-series first and per-MAC drill-downs second.
2. **Failure isolation** is non-negotiable for a daemon whose primary job is to keep an unattended WiFi network healthy. A separate container (Option 1) cannot stall the kicker; in-process (Option 2) can. ADR-0001 already designed the daemon to be a single-loop, single-purpose process — keeping that property is worth one extra container.
3. Option 1 is **additive, not exclusive**. A Prometheus exporter (Option 3) for trend gauges and HA MQTT discovery (Option 5) for phone-app surfacing can be layered on later as their own ADRs without touching the sidecar's design. Picking Option 3 or Option 5 *first* would force a re-architecture once the device-history list is needed.
4. **Read-only by construction** matches the v1 scope. No write paths means no auth-bypass risk, no allowlist drift between UI and config file, no race between SIGHUP reloads and UI mutations. When a future ADR adds write paths, it can do so deliberately.
5. **Matches `PLAN.md` §12 verbatim.** The plan was already written with this architecture in mind — Option 1 is the path the spec implicitly endorses.

**Implementation forks resolved by this ADR:**

- **Auth model:** optional bearer token via `WIFI_SHEPARD_UI_TOKEN` env var. Unset → no auth (homelab default). No login form, no session cookies in v1.
- **Read-model location:** a dedicated `src/wifi_shepard_ui/views.py` module owns every SQL query the UI runs. The daemon's `db.py` schema is the only contract; the UI does not import from `wifi_shepard.*` Python modules.
- **Routing:** three routes in v1 — `GET /` (overview), `GET /devices` (list), `GET /devices/{mac}` (history). No `POST`/`PUT`/`DELETE`.
- **Templating:** Jinja2 server-side rendering. HTMX is a `<script>` tag pulled from a CDN-pinned version (or vendored static asset), used for refreshing overview tiles and inline drilldowns. No JS bundler, no React, no build step.
- **Image size:** `python:3.12-slim` base, same `uv`-managed pyproject pattern as the daemon. New image, same conventions.
- **Port:** `8080` inside the container; mapped or routed via the existing reverse-proxy stack (`docker-compose.infra.yml`) — the compose fragment publishes `expose: ["8080"]`, not `ports:`, by default.
- **Deferred to follow-up ADRs:** allowlist edits from the UI, force-unquarantine button, threshold-tuning UI, Prometheus exporter, HA MQTT discovery.

## Acceptance Criteria

- [ ] **AC-1**: Given the new compose fragment is included in `docker-compose.local.yml`, when `dca up -d wifi-shepard-ui` runs, then a container `wifi-shepard-ui` builds from `Dockerfile.ui`, starts, mounts `/data:ro`, and `dca ps` shows it healthy.
- [ ] **AC-2**: Given the UI container is running and `/data/state.db` contains rows, when an operator GETs `/devices`, then the response is a server-rendered HTML table where each row is one MAC with: total kick count, last-bad-window timestamp, current backoff state, allowlist flag — sortable by clicking a column header.
- [ ] **AC-3**: Given a MAC with at least one row in `kick_events` and `client_samples`, when an operator GETs `/devices/{mac}`, then the response is a chronological timeline merging both tables, oldest at the bottom, including dry-run rows visually distinguished from real kicks.
- [ ] **AC-4**: Given the UI container is running, when an operator GETs `/`, then the response shows: total tracked clients, currently-quarantined count, kicks-today, kicks-this-week, and the top 5 APs by current `cu_total`.
- [ ] **AC-5**: Given `/data/state.db` is mounted read-only and the UI's SQLite connection uses `file:/data/state.db?mode=ro`, when the daemon writes a new row, then any UI write attempt fails with `SQLITE_READONLY` and the UI does not attempt write paths anywhere.
- [ ] **AC-6**: Given the UI source tree, when `grep -rE "@app\.(post|put|delete|patch)" src/wifi_shepard_ui/` runs, then it returns zero matches in v1.
- [ ] **AC-7**: Given `WIFI_SHEPARD_UI_TOKEN` is set in the env, when an operator GETs any route without a matching `Authorization: Bearer ...` header, then the response is `401 Unauthorized`. When the env var is unset, all routes respond `200`.
- [ ] **AC-8**: Given `/data/state.db` does not yet exist (fresh deploy, daemon not yet started), when the UI starts and an operator GETs `/`, then the UI renders an empty-state page (no data yet) instead of crashing or returning 5xx.

## Consequences

### Positive

- The kicker's failure domain is preserved. UI bugs cannot stop the daemon kicking.
- Three concrete views (`/`, `/devices`, `/devices/{mac}`) cover the user's stated needs; nothing is overbuilt.
- Read-only construction means there is no v1 surface to compromise — no auth bypass, no input validation regressions, no SIGHUP/UI race.
- Schema coupling is contained in `views.py`. A schema change is a discoverable, single-file diff.
- Future Prometheus/HA/Grafana integrations layer on top — none of this work is wasted if the homelab later wires up Grafana too.

### Negative

- One more container, one more image to maintain. `dca pull` adds a node.
- We own a small piece of web UI. HTMX + Jinja is light, but it's not free.
- "Read-only" defers a real operator need (force-unquarantine, allowlist edits) to a future ADR. Operators may push for those before this ADR is even Accepted.
- Schema coupling is real. A daemon DB migration that renames a column silently breaks the UI until `views.py` is updated.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Daemon schema change silently breaks UI | Medium | Medium | UI startup runs a smoke-test query against every table column it depends on; fails fast and logs which column is missing. CI in the daemon repo runs the UI image's smoke test against the daemon's migrated schema. |
| WAL reader contention causes UI 500s during heavy poll cycles | Low | Low | UI uses `aiosqlite` with `mode=ro`; queries are bounded by `LIMIT` clauses; no long-running scans without index. |
| Operator exposes the UI publicly without setting `WIFI_SHEPARD_UI_TOKEN` | Medium | Medium | README and `env.example` call out the token. The compose fragment publishes `expose:`, not `ports:`, so the operator must consciously route it through the reverse proxy. UI logs a `WARN` line on startup when the token is unset. |
| Read-only constraint becomes operator pain (e.g. forgot to allowlist a device, can't fix in UI) | Medium | Low | Documented as a v1 limitation. Mitigated by SIGHUP reload taking effect within one poll cycle. A follow-up ADR adds write paths once the patterns are clearer. |
| HTMX from a CDN goes down or version-drifts | Low | Low | Vendor a pinned copy under `static/` instead of CDN. CSP headers restrict to `self`. |
| UI image bloats the homelab's `dcpa` failure surface | Low | Low | `python:3.12-slim` base, no build step, same conventions as the daemon. Pin tags. |

## Implementation Plan

Build order optimized so each phase ends with something runnable and testable in the homelab.

- [ ] **Phase 0 — scaffolding**: new tree at `src/wifi_shepard_ui/` (parallel to `src/wifi_shepard/`); fresh `pyproject_ui.toml` (or a second `[project]` in the existing pyproject if `uv` workspace mode supports it cleanly); `Dockerfile.ui`; new `docker-compose.fragment.yml` block with `expose: ["8080"]`, `volumes: - ./volumes/wifi-shepard:/data:ro`, `env_file: ./env/wifi-shepard-ui.env`.
- [ ] **Phase 1 — read model**: `src/wifi_shepard_ui/views.py` — every SQL query the UI runs lives here as an async function returning a typed dataclass. Unit tests at `tests/test_ui_views.py` against a fixture DB.
- [ ] **Phase 2 — overview + health**: `GET /` route, Jinja template, empty-state handling for missing-DB case (AC-8). Smoke test in the UI container's startup script.
- [ ] **Phase 3 — device list**: `GET /devices` with sortable headers (HTMX swap on click), allowlist flag, current backoff state pulled from `mac_state`.
- [ ] **Phase 4 — device history**: `GET /devices/{mac}` chronological timeline, dry-run rows visually distinguished, pagination if > 200 rows.
- [ ] **Phase 5 — auth**: bearer-token middleware reading `WIFI_SHEPARD_UI_TOKEN`. Tests for the set / unset / mismatched-token cases.
- [ ] **Phase 6 — schema-drift smoke test**: startup-time assertion that every column referenced in `views.py` exists in the SQLite catalog; fail fast with a clear error.
- [ ] **Phase 7 — monorepo deploy**: append the `wifi-shepard-ui:` block to `/media/cubxi/docker/docker-compose.local.yml`; create `./env/wifi-shepard-ui.env`. Document in `README.md` and `CLAUDE.md` how to reach it (reverse-proxy hostname, optional token).
- [ ] **Phase 8 — graduation**: after ≥1 week of operation, move `wifi-shepard-ui:` to `docker-compose.home.yml` (same as the daemon graduates).

## Related ADRs

- [ADR-0000 (index)](./0000-adr-index.md)
- [ADR-0001](./0001-mvp-scope-base-feature.md) — defines the SQLite schema this UI reads.

Anticipated follow-ups (not yet written):

- ADR for **UI write paths** — allowlist edits from the UI, force-unquarantine, threshold tuning. Auth model gets stricter once writes exist.
- ADR for **Prometheus `/metrics` exporter** — additive to this UI, not a replacement. Targets the time-series questions (kick rate trend, AP saturation trend) that the sidecar's tabular views cover poorly.
- ADR for **HA MQTT discovery** — additive to this UI; surfaces per-device sensors in HA for phone-app alerts and automation triggers.
- ADR for **Floor-plan / roam-map visualization** (`PLAN.md` §12) — extension of this UI once the v1 views are in operator hands.

## References

- [`PLAN.md`](../../PLAN.md) §12 ("UI (way down the road)").
- [`CLAUDE.md`](../../CLAUDE.md) (project conventions and stack).
- [`/media/cubxi/docker/CLAUDE.md`](../../../../CLAUDE.md) (monorepo `dca` wrapper, stack-file conventions, in-progress vs stable graduation).
- [FastAPI](https://fastapi.tiangolo.com/) — server framework.
- [HTMX](https://htmx.org/) — progressive-enhancement library, no build step.
- [SQLite URI filename `mode=ro`](https://www.sqlite.org/uri.html) — read-only connection guard.
