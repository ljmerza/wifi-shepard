# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`wifi-shepard` is a long-running Docker daemon that watches a wireless network and gently re-roams misbehaving clients so cheap IoT (Espressif WLEDs, smart plugs, off-brand cameras) stop monopolizing 2.4 GHz airtime by clinging to the wrong AP.

Built around a brand-agnostic `Controller` interface — UniFi first; Omada / OpenWRT / Ruckus / Aruba slot in as new backend classes without changing the scanner / scorer / actor.

## Status & Source of Truth

**v1 in progress** — the core daemon is implemented and tested (scanner → scorer → actor, dry-run gate, UniFi backend, SQLite, SIGHUP/SIGTERM); a read-only UI sidecar (ADR-0002) ships alongside it. See [`docs/adr/0000-adr-index.md`](./docs/adr/0000-adr-index.md) for what's shipped vs. still open.

For *what's built*, the code and the ADR index are the source of truth. [`PLAN.md`](./PLAN.md) remains the reference for the full v1 spec — detection rules, backoff schedule, config shape, roadmap — but where it diverges from the code, the code wins. Known open gaps are tracked in the ADRs (e.g. the concrete HA reboot executor + device-registry client, ADR-0005/0006).

## Stack (per `PLAN.md` §5)

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Async | `asyncio` (single event loop, single process) |
| Controller backends | `Controller` Protocol; `UniFiController` first via `aiounifi` |
| Local state | SQLite via `aiosqlite` (WAL mode) at `/data/state.db` |
| Config | YAML at `/config/config.yaml`, hand-parsed (`yaml.safe_load` + frozen dataclasses, fail-closed validation) with `${VAR}` env-var interpolation |
| Logging | stdlib `logging` → stdout (level via `WIFI_SHEPARD_LOG_LEVEL`; Docker log driver picks it up) |
| Notifications | Home Assistant REST `/api/services/notify/<service>` |
| Container base | `python:3.12-slim` |
| Lint / format | `ruff` (managed via `uv`) |

No web framework, no UI, no CLI in the **daemon** — it starts → loops → logs → exits cleanly on SIGTERM, observable via `docker ps` + log lines. (A separate read-only web UI sidecar ships as its own image — ADR-0002.)

## Repo layout

```
projects/wifi-shepard/
├── PLAN.md                       # full v1 spec (detection, backoff, roadmap, risks)
├── CLAUDE.md
├── README.md
├── pyproject.toml                # ruff + uv-managed deps; pytest config
├── Dockerfile                    # daemon image (python:3.12-slim, uv)
├── Dockerfile.ui                 # read-only UI sidecar image (ADR-0002)
├── docker-compose.yml            # daemon + UI fragment (merge into the monorepo)
├── env.example
├── config.example.yaml
├── config.yaml
├── docs/adr/                     # architecture decision records (index: 0000)
├── src/
│   ├── wifi_shepard/             # the daemon
│   │   ├── __main__.py           # entry point (`python -m wifi_shepard`)
│   │   ├── main.py               # Daemon: signal handling, top-level loop
│   │   ├── config.py             # config dataclasses: YAML loader, env interp, fail-closed validation
│   │   ├── pipeline.py           # composition root: scorer + backoff + rate-limiter + actor
│   │   ├── scanner.py            # per-controller poll loop
│   │   ├── scorer.py             # sliding-window bad-state detection
│   │   ├── resolution.py         # per-MAC override > global threshold/mechanism resolution
│   │   ├── backoff.py            # per-MAC state machine (quarantine)
│   │   ├── rate_limit.py         # global single-flight + per-AP cap (ADR-0004)
│   │   ├── actor.py              # kick gate: BTM→deauth fallback, dry-run, notify
│   │   ├── pending.py            # in-flight BTM / post-kick roam-check bookkeeping
│   │   ├── db.py                 # aiosqlite (WAL): client_samples, kick_events, reboot_events
│   │   ├── notify/               # Notifier Protocol + HA REST backend (home_assistant.py)
│   │   ├── controllers/          # base.py Protocol, unifi.py, __init__.py factory
│   │   └── reboot/               # ADR-0005/0006: eligibility, ha_resolver, cooldown, scheduler, rebooter
│   └── wifi_shepard_ui/          # ADR-0002 read-only sidecar (FastAPI app, views, templates)
├── tests/                        # pytest; AC-named (`test_*_acN.py`); `tests/ui/` for the sidecar
└── .github/workflows/            # CI: pytest + docker build (daemon + UI), release to GHCR
```

## Brand-agnostic Controller

Every backend implements the `Controller` Protocol declared in `src/wifi_shepard/controllers/base.py`:

```python
async def list_wireless_clients(self) -> list[ClientSnapshot]: ...
async def list_aps(self) -> list[APSnapshot]: ...
async def get_ap_radio_stats(self, ap_id: str) -> list[RadioStats]: ...
async def force_reconnect_client(self, mac: str) -> None: ...
async def send_btm_request(self, mac: str, target_bssid: str | None = None) -> None: ...  # optional per vendor
```

The scanner / scorer / actor never know which vendor they're talking to. New backends are new classes plus a `type:` entry in `controllers:` in `config.yaml`. **Don't push vendor specifics into the scorer.**

## Configuration & Secrets

- Config file: YAML mounted at `/config/config.yaml` (read at startup, re-read on `SIGHUP`).
- Schema: see `PLAN.md` §7. Global `detection:` defaults plus per-MAC `overrides:` — resolution is **per-MAC override > global default**, applied uniformly to every threshold.
- Allowlist (`allowlist:`) — MACs in this list are never kicked.
- Secrets in env vars, never in repo: `UNIFI_PASSWORD`, `HA_TOKEN`. Loaded from `./env/wifi-shepard.env` and interpolated into the YAML at parse time (`${VAR}` syntax).
- Fail closed: invalid config → log a clear error and exit. Never half-run.

## Deployment in the docker monorepo

Per [`/media/cubxi/docker/CLAUDE.md`](../../CLAUDE.md), in-progress services live in `docker-compose.local.yml` and graduate to `docker-compose.home.yml` once stable.

```yaml
wifi-shepard:
  build: ./projects/wifi-shepard
  container_name: wifi-shepard
  restart: unless-stopped
  env_file:
    - ./env/global.env
    - ./env/wifi-shepard.env
  volumes:
    - ./volumes/wifi-shepard:/data
    - ./projects/wifi-shepard/config.yaml:/config/config.yaml:ro
  logging: *default-logging
```

Reuse the `*default-logging` anchor from `docker-compose.base.yml` rather than redefining it. No exposed ports — observability is logs only in v1.

## Common commands

Work goes through the monorepo's `dca` wrapper (all-stacks `docker compose`). From the monorepo root:

```bash
dca config                          # validate the merged compose graph
dca up -d wifi-shepard              # start the daemon
dca logs -f wifi-shepard            # follow logs
dca restart wifi-shepard            # restart after config edit
dca pull wifi-shepard && dca up -d wifi-shepard  # pull + recreate
```

Local dev (inside `projects/wifi-shepard/`):

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ruff format .
```

## Architecture Decisions

ADRs live in [`docs/adr/`](./docs/adr/). The index is [`0000-adr-index.md`](./docs/adr/0000-adr-index.md).

- Create a new ADR: invoke `/adr <topic>` — the skill walks through options, records the decision, and appends a row to the index.
- Implement an Accepted ADR: `/adr-to-pr docs/adr/NNNN-slug.md` — TDD-driven PR generation against the ADR's `AC-N` acceptance criteria.

Decisions still anticipated (not yet written): notification channels beyond Home Assistant, Prometheus / OpenTelemetry metrics shape, MQTT-discovery for HA entities, and a second `Controller` backend (Omada / OpenWRT) to prove the Protocol's portability. (Already decided: `Controller` shape, kick mechanism → ADR-0003, rate limits → ADR-0004, reboot backend → ADR-0005/0006, persistence schema + threshold-resolution → ADR-0001.)

## Documentation

- [`PLAN.md`](./PLAN.md) — full v1 spec, detection rules, backoff schedule, roadmap, risks.
- [`docs/adr/`](./docs/adr/) — architecture decision records.
- [`/media/cubxi/docker/CLAUDE.md`](../../CLAUDE.md) — monorepo conventions (`dca`, stack files, env / volume patterns).
