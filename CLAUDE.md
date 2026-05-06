# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`wifi-shepard` is a long-running Docker daemon that watches a wireless network and gently re-roams misbehaving clients so cheap IoT (Espressif WLEDs, smart plugs, off-brand cameras) stop monopolizing 2.4 GHz airtime by clinging to the wrong AP.

Built around a brand-agnostic `Controller` interface — UniFi first; Omada / OpenWRT / Ruckus / Aruba slot in as new backend classes without changing the scanner / scorer / actor.

## Status & Source of Truth

**Greenfield, pre-v0** — no source code yet, only the spec.

Until v1 ships, [`PLAN.md`](./PLAN.md) is the canonical source of truth for scope, detection rules, backoff schedule, configuration shape, repo layout, and roadmap. This file captures only what is stable and useful for orienting a Claude Code session; do not duplicate `PLAN.md` here.

When code lands, prune `PLAN.md` references in this file and replace them with concrete runtime / commands / module pointers.

## Stack (planned, per `PLAN.md` §5)

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Async | `asyncio` (single event loop, single process) |
| Controller backends | `Controller` Protocol; `UniFiController` first via `aiounifi` |
| Local state | SQLite via `aiosqlite` (WAL mode) at `/data/state.db` |
| Config | YAML at `/config/config.yaml`, parsed by `pydantic-settings` with env-var interpolation |
| Logging | `structlog` → stdout (Docker log driver picks it up) |
| Notifications | Home Assistant REST `/api/services/notify/<service>` |
| Container base | `python:3.12-slim` |
| Lint / format | `ruff` (managed via `uv`) |

No web framework, no UI, no CLI in v1. Container starts → loops → logs → exits cleanly on SIGTERM. Health is observable via `docker ps` + log lines.

## Repo layout (planned, per `PLAN.md` §9)

```
projects/wifi-shepard/
├── PLAN.md
├── CLAUDE.md
├── README.md                    # operating runbook (after v1)
├── pyproject.toml               # ruff + uv-managed deps
├── Dockerfile
├── docker-compose.fragment.yml  # to merge into docker-compose.local.yml
├── env.example
├── config.example.yaml
├── docs/
│   └── adr/                     # architecture decision records
├── src/wifi_shepard/
│   ├── __init__.py
│   ├── main.py                  # entry point, signal handling, top-level loop
│   ├── config.py                # pydantic-settings, YAML loader, env interp
│   ├── controllers/
│   │   ├── base.py              # Controller Protocol, ClientSnapshot model
│   │   ├── unifi.py             # UniFiController (aiounifi)
│   │   └── __init__.py          # backend factory keyed on YAML "type"
│   ├── scanner.py               # poll loop
│   ├── scorer.py                # bad-state detection, threshold resolution
│   ├── backoff.py               # per-MAC state machine
│   ├── actor.py                 # force_reconnect call
│   ├── notify/
│   │   └── ha.py
│   ├── db.py                    # aiosqlite session, migrations
│   └── models.py                # pydantic models
└── tests/
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

Once code exists, work goes through the monorepo's `dca` wrapper (all-stacks `docker compose`). From the monorepo root:

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

Decisions worth their own ADR (anticipated, not yet written): `Controller` protocol shape, kick mechanism (deauth vs 802.11v BTM vs 802.11k assist), notification channels, persistence schema, dry-run/observe-only graduation criteria, threshold-resolution semantics, Prometheus / OpenTelemetry shape, MQTT-discovery for HA entities.

## Documentation

- [`PLAN.md`](./PLAN.md) — full v1 spec, detection rules, backoff schedule, roadmap, risks.
- [`docs/adr/`](./docs/adr/) — architecture decision records.
- [`/media/cubxi/docker/CLAUDE.md`](../../CLAUDE.md) — monorepo conventions (`dca`, stack files, env / volume patterns).
