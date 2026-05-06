# ADR-0001: MVP Scope for the Base Feature (dry_run-gated v1)

**Status:** Proposed
**Date:** 2026-05-05
**Author:** Leonardo Merza

## Context

### Background

`wifi-shepard` is a long-running daemon that watches a wireless network and force-reconnects misbehaving 2.4 GHz clients (see [`PLAN.md`](../../PLAN.md)). The project is greenfield — no source code yet. Before code lands we need an MVP scope that is small enough to build in a weekend, but structured so that graduating from "observe only" to "actually kicking" is a config flip, not a rewrite.

[`PLAN.md`](../../PLAN.md) §11 sketches v0 (observe-only, ~1–2 evenings) and v1 (actions on, ~a weekend) as separate phases. This ADR resolves whether the MVP is v0, v1, or a unified shape that subsumes both.

### Current State

- `PLAN.md` exists; `CLAUDE.md` and `docs/adr/0000-adr-index.md` were just bootstrapped.
- No `pyproject.toml`, `Dockerfile`, or `src/` tree.
- `PLAN.md` §7 already specifies `dry_run: true` as a top-level config flag with the comment *"start safe; flip to false when you trust it"*. The plan was written assuming a single binary, two operational modes.

### Requirements

1. Daemon polls a UniFi controller, scores wireless clients against the detection rules in `PLAN.md` §3, and decides per-MAC whether to act.
2. With `dry_run: true` (default), the daemon must **never** call the controller's force-reconnect endpoint, but must log every action it would have taken with enough detail to evaluate the decision after the fact.
3. With `dry_run: false`, the daemon executes force-reconnects, records kick events in SQLite, applies the backoff state machine in `PLAN.md` §4, and sends a Home Assistant notification per kick and per quarantine.
4. Per-MAC overrides in YAML take precedence over global defaults for every threshold.
5. Allowlisted MACs are never kicked (and never logged as `WOULD KICK`).
6. Quiet hours apply unless the device meets the stricter `override_threshold` set.
7. The daemon reloads its config on `SIGHUP` and exits cleanly on `SIGTERM`.
8. Invalid config at startup or post-reload is rejected with a clear error; the previous config remains active on a failed reload (fail closed).
9. UniFi is the only backend in MVP, but the `Controller` Protocol must be in place so a second backend is purely additive.

### Constraints

- One Python process, no Celery/Redis/supervisord (`PLAN.md` §5).
- Runs in the existing `/media/cubxi/docker` monorepo via `docker-compose.local.yml` (see [`CLAUDE.md`](../../CLAUDE.md) and [`/media/cubxi/docker/CLAUDE.md`](../../../../CLAUDE.md)).
- Secrets only via env vars (`UNIFI_PASSWORD`, `HA_TOKEN`); none in the YAML.
- Must tolerate aiounifi API drift across UniFi controller versions: pin a version, fail closed on schema mismatch.

## Options Considered

### Option 1: dry_run-gated v1 (Chosen)

**Description:** Build the full `PLAN.md` v1 — Controller Protocol, scanner, scorer with per-MAC override resolution, backoff state machine, quarantine, allowlist, quiet hours, HA per-event notifications, SQLite WAL persistence, SIGHUP reload — and gate the actor's force-reconnect call on `config.dry_run`. Default: `true`. The MVP ships running with the flag on; v1 graduation is a config edit.

**Pros:**
- Single binary, single migration path. v0 and v1 are the same code; the gate is one `if` in `actor.py`.
- The backoff state machine, override resolution, and notification path are exercised in dry-run, so bugs surface before they cost a real kick.
- Matches `PLAN.md` §7's `dry_run` config field as designed — no rework of config schema later.
- Fast operator graduation: edit YAML → SIGHUP → live. No redeploy.

**Cons:**
- ~weekend of work upfront vs ~1–2 evenings for pure v0.
- Backoff state machine complexity exists even when actions are off.

### Option 2: Observer-only (v0 only)

**Description:** Daemon polls + scores + logs `would-kick` decisions. No HA, no actor, no backoff machine. `PLAN.md` v1 is built later as a separate phase.

**Pros:**
- Shortest path to running against real data.
- Clean separation between data collection and decision-making.

**Cons:**
- Two phases of work; v1 phase will likely refactor scanner/scorer once backoff and actor needs surface.
- Threshold tuning happens against logs without ever exercising the action path — bugs in actor/backoff surface only at v1, when stakes are higher.
- The dry_run flag in `PLAN.md` §7 becomes vestigial during v0.

### Option 3: Observer + HA digest

**Description:** Option 2 plus an hourly HA digest notification ("N devices flagged, top offenders: ..."). Wires the HA REST path early without enabling kicks.

**Pros:**
- Operator visibility immediately, even before kicks are trusted.
- Validates the HA path (token, notify_service name, network reachability) before the first real kick.

**Cons:**
- Digest format is throwaway — per-event notifications need different wiring; this path is rebuilt at v1.
- Still defers the backoff state machine to a later phase.

### Option 4: Single-device end-to-end slice

**Description:** Hardcode one target MAC, run the full poll → score → kick → notify loop only on it. Flat 5-min cooldown, no backoff machine, no Controller Protocol abstraction.

**Pros:**
- Fastest end-to-end demonstration. Smallest possible "yes the kick mechanism works against my UDM Pro."
- Useful as a 30-minute spike to de-risk `aiounifi.force_reconnect()`.

**Cons:**
- Throwaway architecture: scaling from one MAC to N requires rewriting scanner, scorer, and actor.
- No Controller Protocol → second-backend work compounds with rewrite.
- Bad fit for "MVP"; better treated as a pre-MVP de-risking spike if at all.

## Decision

**Chosen Option:** Option 1 — dry_run-gated v1.

**Rationale:**

1. `PLAN.md` was already written around a unified-binary model (the `dry_run` flag in §7, the v0→v1 staging in §11). Option 1 is the path the spec implicitly endorses; the others fight it.
2. Exercising backoff, override resolution, and the notification pipeline during the dry-run period is the cheapest way to find bugs in those subsystems. Pure v0 (Options 2/3) defers those bugs to the moment they would cost a real kick-loop.
3. Operator graduation is a 60-second config edit + SIGHUP, not a redeploy or a code branch merge. This matters because the user explicitly wants to validate thresholds in the wild for ~1 week before enabling kicks (`PLAN.md` §11 v0).
4. Building the `Controller` Protocol upfront keeps the second backend (Omada/OpenWRT) additive — a separate ADR adds a class plus a YAML `type:` entry, nothing else.

**Implementation forks resolved by this ADR:**

- **Kick mechanism:** `force_reconnect_client` (deauth) only. `send_btm_request` (802.11v BTM) is **deferred to a future ADR**. The Protocol declares `send_btm_request` as optional, but no backend implements it in MVP.
- **Notifications:** per-event HA notify on kick **and** on first entry into QUARANTINE. No digest mode in MVP.
- **Persistence:** SQLite WAL at `/data/state.db` with two tables: `client_samples` (every poll cycle) and `kick_events` (every action, including dry-run "would-kick" rows so post-MVP tuning can replay decisions).
- **SIGHUP reload:** in MVP scope. Failed reload keeps the previous config (fail closed on the new one).
- **Logging:** structured JSON to stdout in production, human-readable when `log_format: human`. `UNIFI_PASSWORD` and `HA_TOKEN` are never logged (redact at logger config).

## Acceptance Criteria

- [ ] **AC-1**: Given a valid `config.yaml` and reachable UniFi controller, when the daemon starts, then it polls `list_wireless_clients()` every `scanner.poll_interval_seconds` (default 60) and writes one `client_samples` row per client per poll into SQLite.
- [ ] **AC-2**: Given `scanner.dry_run: true` and a client whose sliding window meets the bad-state criteria, when the scan cycle completes, then the daemon logs a structured `would_kick` event (with `mac`, reason fields, and the resolved thresholds used) and does NOT call `Controller.force_reconnect_client`.
- [ ] **AC-3**: Given `scanner.dry_run: false`, a client meeting bad-state criteria, and backoff state allowing action, when the scan cycle completes, then the daemon calls `Controller.force_reconnect_client(<mac>)` exactly once, inserts a `kick_events` row, sends one HA notification, and advances backoff state for that MAC.
- [ ] **AC-4**: Given a MAC listed in `allowlist:`, when any scan cycle evaluates it, then the daemon never logs `would_kick` and never calls `force_reconnect_client`, regardless of metrics or `dry_run` setting.
- [ ] **AC-5**: Given a MAC that has been kicked `backoff.quarantine_after_kicks` times (default 5), when the next bad-state window is detected, then the MAC enters QUARANTINE, no further kicks are attempted for that MAC, and exactly one HA notification with severity "quarantine" is sent.
- [ ] **AC-6**: Given a global `detection.tx_rate_kbps_max: 12000` and an `overrides:` entry with `mac: X` and `tx_rate_kbps_max: 6000`, when MAC `X` is scored, then the threshold used is 6000; when any other MAC is scored, the threshold used is 12000.
- [ ] **AC-7**: Given a running daemon, when `SIGHUP` is received and the new `config.yaml` parses successfully, then the new config is applied to the next scan cycle; when it fails to parse, the daemon logs a clear error and continues running with the previous config.
- [ ] **AC-8**: Given a running daemon mid-scan, when `SIGTERM` is received, then the in-flight scan finishes (or aborts cleanly within 5s), open SQLite/HA/UniFi connections close, and the process exits with code 0.

## Consequences

### Positive

- One binary, one config schema, one deploy. v0→v1 is `dry_run: false` + `SIGHUP`.
- All non-actor subsystems (scorer, backoff, override resolution, notifier, persistence) are exercised against real data during the dry-run period — surfacing bugs before they cost real airtime.
- The `Controller` Protocol is in place from commit one; second-backend work (Omada, OpenWRT) is a separate ADR adding one class plus a `type:` entry, not a refactor.
- Per-event HA notifications double as an operator audit trail in the dry-run period (every "would-kick" decision shows up in their phone log if they wire it that way — though in MVP only real kicks notify; dry-run decisions are stdout-only).

### Negative

- ~weekend of upfront build vs ~1–2 evenings for pure observer-only v0.
- Carries the backoff state machine complexity even while actions are gated off.
- More surface area for the first PR — slower initial review.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `aiounifi` API drift across UniFi controller versions | Medium | High | Pin `aiounifi` in `pyproject.toml`. Record a fixture of the UniFi response. Schema-validate at parse time; fail closed if the response shape changes. |
| Backoff state machine bug → kick-loop on a defective device | Medium | High | Quarantine cap (default 5). `max_kicks_per_hour: 3` and `max_kicks_per_day: 10` hard caps. Dry-run period catches loop-prone devices in logs first. |
| Operator flips `dry_run: false` before tuning thresholds | Medium | High | `dry_run: true` is the default for the missing-key case (not just the example value). README/CLAUDE.md call out the recommended ≥1 week observe period. |
| HA token leakage in logs | Low | Medium | Redact `HA_TOKEN` and `UNIFI_PASSWORD` at logger config; never log full request headers. |
| SQLite WAL write contention | Low | Low | One writer (the daemon). Polling cadence (60s) keeps insert rate bounded. |
| UniFi controller rate-limits the daemon during a kick storm | Low | Medium | Single-flight kicks (deferred to a follow-up ADR if needed); per-day caps already gate volume. |

## Implementation Plan

Build order optimized so each phase ends with something runnable and testable.

- [ ] **Phase 0 — scaffolding**: `pyproject.toml` (uv-managed, ruff + pytest), `Dockerfile` (`python:3.12-slim`), `config.example.yaml`, `env.example`, `docker-compose.fragment.yml` for monorepo merge.
- [ ] **Phase 1 — config + models**: `src/wifi_shepard/config.py` (pydantic-settings, env-var interpolation, validation), `src/wifi_shepard/models.py` (`ClientSnapshot`, `APSnapshot`, `RadioStats`, `KickEvent`).
- [ ] **Phase 2 — Controller Protocol + UniFi backend**: `src/wifi_shepard/controllers/base.py` (Protocol + dataclasses), `src/wifi_shepard/controllers/unifi.py` (read-only methods first), `controllers/__init__.py` (factory keyed on `type:`).
- [ ] **Phase 3 — persistence**: `src/wifi_shepard/db.py` (aiosqlite + WAL + initial schema: `client_samples`, `kick_events`, `mac_state`).
- [ ] **Phase 4 — scoring**: `src/wifi_shepard/scorer.py` (sliding window per MAC, per-MAC override resolution, allowlist short-circuit, quiet-hour stricter-threshold logic). Unit tests at `tests/test_scorer.py` and `tests/test_threshold_resolution.py`.
- [ ] **Phase 5 — backoff state machine**: `src/wifi_shepard/backoff.py` (NORMAL → KICK_PENDING → KICKED → EVALUATING → QUARANTINE per `PLAN.md` §4). Unit tests at `tests/test_backoff.py`.
- [ ] **Phase 6 — actor + dry_run gate**: `src/wifi_shepard/actor.py`. Calls `force_reconnect_client` only when `not config.dry_run`. Always writes to `kick_events`; the row's `dry_run` boolean column distinguishes simulation from real.
- [ ] **Phase 7 — notifier**: `src/wifi_shepard/notify/ha.py` (HA REST POST, retry on 5xx, redact token from logs).
- [ ] **Phase 8 — main loop + signals**: `src/wifi_shepard/main.py` (asyncio entry, scanner loop, SIGTERM/SIGHUP handlers, structlog setup).
- [ ] **Phase 9 — integration**: end-to-end smoke against a recorded UniFi response fixture; manual verification against a real UDM Pro in dry-run mode for ≥1 week before flipping `dry_run: false`.
- [ ] **Phase 10 — monorepo deploy**: add the `wifi-shepard:` block to `/media/cubxi/docker/docker-compose.local.yml`; create `./env/wifi-shepard.env` with `UNIFI_PASSWORD` and `HA_TOKEN`.

## Related ADRs

- [ADR-0000 (index)](./0000-adr-index.md)

Anticipated follow-ups (not yet written):

- ADR for **kick mechanism upgrade** — deauth vs 802.11v BTM vs 802.11k assist; per-client capability detection.
- ADR for **second Controller backend** (Omada or OpenWRT) — proves the Protocol's portability.
- ADR for **observability** — Prometheus `/metrics` endpoint vs OpenTelemetry traces vs HA MQTT discovery.
- ADR for **single-flight kicks / global rate-limit** — only if real-world operation shows controller rate-limit pain.

## References

- [`PLAN.md`](../../PLAN.md) §3 (detection criteria), §4 (backoff schedule), §7 (config schema), §11 (v0/v1 roadmap).
- [`CLAUDE.md`](../../CLAUDE.md) (project conventions and stack).
- [`/media/cubxi/docker/CLAUDE.md`](../../../../CLAUDE.md) (monorepo `dca` wrapper, stack-file conventions).
- [`aiounifi` on PyPI](https://pypi.org/project/aiounifi/) — UniFi backend client library.
- [`pydantic-settings`](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — config loader with env-var interpolation.
- [Home Assistant `notify` REST API](https://developers.home-assistant.io/docs/api/rest/) — `/api/services/notify/<service>`.
