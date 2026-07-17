# ADR-0013: Settings UI Write Paths — Editable Config via the Sidecar with Env-Var Secret References and Daemon File-Watch Reload

**Status:** Implemented
**Date:** 2026-07-16
**Author:** Leonardo Merza

> **Implementation note (2026-07-16):** all acceptance criteria are implemented and
> covered by tests on branch `feat/adr-0013-settings-ui`. The only remaining step is
> the operational **Phase 6 graduation** — recreating the live containers with the new
> directory mount + `:rw` config mount (the live `docker-compose.home.yml` is not in
> this repo). See the deployment record for the recreate steps.

## Context

### Background

The daemon is configured entirely through a hand-edited YAML file (`/config/config.yaml`, `PLAN.md` §7), and most of that surface is opaque to anyone who doesn't know WiFi internals. `signal_dbm_max: -70`, `ap_cu_total_min: 60`, `retry_pct_max: 30`, the conjunctive-AND detection model, the escalating `cooldowns_seconds` ladder — none of it explains *what it means* or *when you'd change it*. Tuning today means reading `config.example.yaml`'s comments, hand-editing a gitignored file, and recreating the container.

The ask: make **every** setting editable from the existing web sidecar, with a plain-English description on each field (especially the detection thresholds), so an operator who doesn't live in 802.11 can reason about them. Secrets (UniFi/HA/Pi-hole passwords, API tokens) stay out of the UI — instead the UI lets you name the **env var** that holds the secret (e.g. type `PIHOLE_PASSWORD`), storing a `${PIHOLE_PASSWORD}` placeholder in the YAML. Non-secret values that are *only* in env today because the config referenced them (`UNIFI_HOST`, `UNIFI_USERNAME`, `UNIFI_SITE`, `UNIFI_PORT`) become plain, UI-editable literals and drop out of the env file.

The operator named the model directly: **the same as Frigate** — env vars referenced in the YAML for secrets, and a config-editor UI that stays in sync with the YAML. Frigate keeps secrets in its `config.yml` as env placeholders (`{FRIGATE_*}`) and ships a built-in editor that reads the live file, validates on save, and writes it back. This ADR adopts that pattern with the daemon's existing `${VAR}` placeholder syntax.

### Current State

- **Config** (`config.py`) is `yaml.safe_load` → frozen dataclasses, fail-closed validation in `build_config`. `${VAR}` refs are interpolated over the whole tree at load (`_interpolate_env`), failing closed if a referenced var is unset. `password`/`token` are `repr=False`; a `_SECRET_KEYS`/`_redact` pass keeps them out of error messages. This placeholder mechanism is *exactly* the "env var reference" the operator wants for the UI — it already exists.
- **UI sidecar** (`wifi_shepard_ui/app.py`) is **read-only by construction** (ADR-0002): a runtime fence (`_assert_no_write_routes`, AC-6) rejects any `POST/PUT/DELETE/PATCH` route, it mounts `/data:ro`, and it does **not** mount or read `config.yaml` at all. It even carries a *parallel* `WIFI_SHEPARD_UI_ALLOWLIST` env because it deliberately avoids importing `wifi_shepard.*` — a documented two-places-in-sync gotcha.
- **Reload is broken in the live deployment.** Per the deployment record: (1) PID 1 is `uv run`, which doesn't forward `SIGHUP` to the child daemon; (2) `config.yaml` is a **single-file** bind mount pinned to the inode at container-create time, so an atomic rewrite (write-new + rename → new inode) leaves the container reading the stale file. Today a config change requires `dca up -d --force-recreate wifi-shepard`. The daemon's `_on_sighup` handler itself is correct; the container plumbing defeats it.
- **Some config is consumed only at startup.** `main.py` builds controllers, the HA notifier, and the DNS source **once**; `SIGHUP`/`update_config` retune only detection thresholds, scanner, backoff, quiet hours, and the reboot scheduler. Editing a UniFi host or a Pi-hole URL can never take effect without a restart.
- **Deps** (`pyproject.toml`): the daemon depends on `pyyaml`; the `ui` extra adds `fastapi`/`jinja2`/`uvicorn`. `Dockerfile.ui` copies the whole `src/` tree (both packages) and runs `uv sync --extra ui`, which installs the base deps too — so `wifi_shepard.config` is importable inside the UI container (no new runtime dep needed to reuse the validator).
- **`config.yaml` is gitignored / live-only.** `config.example.yaml` is the tracked, comment-rich template and is never rewritten by the daemon or UI.

### Requirements

1. **Every setting editable from the UI** — the full surface: detection (thresholds, radios, `ap_cu_total_min`, inactivity, dns_thrash), scanner, backoff, safety_rails, quiet_hours, reboot (+ nested proactive/reactive/probe/cooldown/eligible/overrides), per-MAC `overrides[]`, `allowlist`, `controllers[]`, `home_assistant`, `dns_sources[]` (+ instances).
2. **A plain-English description on every field**, written for someone who doesn't know WiFi — what the knob does, which direction is stricter/looser, and a concrete "≈ two rooms away" style anchor for the RF thresholds.
3. **Secrets stay out of the UI.** For each secret field the UI accepts/stores an **env var name**, round-tripped as a `${NAME}` placeholder. The resolved secret is never read, rendered, stored, or sent to the browser.
4. **Non-secret env refs become literals.** `UNIFI_HOST/USERNAME/SITE/PORT` move into `config.yaml` as plain values, editable in the UI, and are removed from `env.example` / the live env file. Only true secrets remain as env vars.
5. **UI ↔ YAML sync (bidirectional).** The UI is a *view over* `config.yaml`: it reflects the current on-disk file (so an out-of-band hand edit shows up) and writes edits back to that same file — one source of truth, not a shadow store.
6. **Fail-closed on save.** An invalid edit is rejected using the daemon's own validation, with its error message, and the on-disk file is left unchanged. The UI must never write a config the daemon would crash on.
7. **Reload without a restart where possible.** After a save, a live-reloadable change takes effect within one cycle with no restart and no manual `SIGHUP`. Startup-only fields are clearly labeled "applies on restart."
8. **Failure isolation preserved (ADR-0002's non-negotiable).** A UI bug — bad save, crash, lock — must never stall the kicker.
9. **Opt-in / no regression.** With the UI unused, the daemon behaves exactly as today; the existing suite stays green.

### Constraints

- One asyncio process for the daemon; **no web framework in the daemon** (ADR-0001/0002). The write path cannot live there.
- The sidecar is the failure-isolation boundary — keep it a separate container.
- `config.yaml` is live-only and comment-rich in practice; a rewrite must not silently destroy operator comments or reorder the file into noise.
- Secrets must never transit the browser or the UI's logs. The `${VAR}` placeholder is resolved **only** in the daemon's environment, at load.
- Match the repo's fail-closed, ADR-per-decision discipline; this decision **amends ADR-0002's read-only constraint** and must say so explicitly.

## Options Considered

### Option 1: UI writes `config.yaml`; daemon auto-reloads on file change (Chosen)

**Description:** The sidecar renders a form from a declarative settings schema, validates a proposed config with the daemon's own `build_config`, and writes `config.yaml` in place (preserving `${VAR}` placeholders and comments). The daemon gains a file-watch task that reloads through the existing `update_config` path when the file changes. The Frigate model, with the daemon's `${VAR}` syntax.

**Pros:**
- No HTTP server added to the daemon — it stays a single-purpose process; the write path lives in the already-web sidecar.
- Failure isolation intact: a UI crash/lock can't stall the kicker (separate container; daemon only *reads* the file).
- **Fixes the broken-`SIGHUP` reload as a side effect** — a directory bind-mount + content-watch sees new inodes and needs no cross-container signal.
- One source of truth stays `config.yaml`; the UI is a pure view over it (Req 5). Reuses the daemon's validator verbatim (Req 6), no schema drift.
- Env-var-reference secrets fall straight out of the existing `${VAR}` mechanism — the UI never handles a secret.

**Cons:**
- The sidecar gains a `:rw` mount on the config dir — the exact thing ADR-0002 forbade (this ADR lifts that, deliberately).
- Round-tripping a comment-rich YAML needs a round-trip serializer (likely `ruamel.yaml` in the `ui` extra) or accepts comment loss with `yaml.safe_dump`.
- A file-watch loop is new daemon surface (small, dependency-free if it polls a content hash).

### Option 2: Daemon exposes a small control API; UI POSTs to it; daemon writes + reloads itself

**Description:** Add a minimal HTTP listener to the daemon (`PUT /config`, `POST /reload`). The UI POSTs proposed settings; the daemon validates, writes, and reloads in-process.

**Pros:**
- Cleanest ownership — the daemon validates, writes, and reloads atomically, in-process; no inode games, no shared writable mount.
- The UI touches no files and needs no `config.yaml` mount.

**Cons:**
- Puts a web server **inside the kicker** — precisely what ADR-0001/0002 avoided to keep it single-purpose; grows the attack surface of the one process that already talks to the WiFi controller.
- Erodes failure isolation: a slow/leaky handler shares the daemon's event loop (ADR-0002's stated reason for a sidecar in the first place).
- Most work; a second, redundant serialization/validation entry point to maintain.

### Option 3: UI writes a separate `settings.yaml` overlay; daemon merges it over `config.yaml`

**Description:** The UI writes only a machine-owned `settings.yaml` holding the operator-tunable subset; the daemon loads `config.yaml` (base: secrets + wiring) and overlays `settings.yaml` on top.

**Pros:**
- The comment-rich, secret-bearing base file is never rewritten — no round-trip/comment-loss problem, no secret near the writer.
- Clean separation of "operator knobs" vs "base wiring."

**Cons:**
- Two config sources and precedence rules to reason about ("why is this value here not there?") — new merge logic in the loader and a new failure mode (base vs overlay disagree).
- Breaks Req 5's single-source-of-truth intent; the UI no longer shows *the* config, it shows one layer of it.
- Still needs the same reload trigger as Option 1 — it doesn't avoid the hard part, only adds a layer.

## Decision

**Chosen Option:** Option 1 — the sidecar edits `config.yaml` directly (validated by the daemon's own parser), and the daemon file-watches for changes and hot-reloads.

**Rationale:** Option 1 keeps the two properties ADR-0001/0002 fought for — **no web server in the daemon** and **failure isolation via the container boundary** — while giving the operator exactly the Frigate-style "edit the YAML through a UI that stays in sync with it" they asked for. It reuses the `${VAR}` placeholder mechanism (secrets never enter the UI) and the `build_config` validator (no drift, no second contract), so the net-new code is a settings schema + form rendering + a round-trip writer + a small daemon watcher. As a bonus it **repairs the reload path** that is currently broken in production. Option 2 would surrender failure isolation and the daemon's single-purpose design; Option 3 adds a second config source and merge semantics for no gain the chosen option doesn't already provide.

**Forks resolved:**

- **A declarative settings schema is the single source of truth.** One module enumerates every editable field as `{yaml_path, kind, default, constraints (range/enum/nullable-to-disable), secret: bool, restart_required: bool, description}`. It drives form rendering, client-side hints, and serialization. A test asserts every field reachable from the daemon's `Config` dataclasses is either in the schema or on an explicit exclusion list — so a future config field can't silently become un-editable. Descriptions are written for a non-expert; the RF thresholds carry a concrete anchor (e.g. `signal_dbm_max: -70` → "how weak a device's signal must be before it counts as 'bad' — about −70 dBm is two rooms from the AP; *more negative = weaker*; only devices weaker than this are even eligible to be nudged"). The conjunctive-AND model and the ADR-0009 `null`-disables convention are surfaced as section-level help, not just per-field.
- **Secrets are env-var references, resolved only in the daemon.** For each secret field the form shows/accepts an env var **name**; the UI stores `${NAME}` and displays `NAME`. The UI never interpolates, never reads the secret's value, never renders or logs it. Validity of the reference (does the daemon's env actually define `NAME`?) is checkable only by the daemon at load — the UI lints the `${UPPER_CASE}` *form* and leaves resolution to the daemon, mirroring Frigate.
- **Validation reuses `build_config`, without interpolation.** On save the UI assembles a config mapping with secret fields carrying their literal `${NAME}` placeholder and calls `build_config(**mapped)` (not `load_config_from_path`, which would try to interpolate against the UI's env). `build_config` accepts `${NAME}` as a non-empty string, so every structural / range / enum / MAC / all-null-detection-trio rule fires exactly as it will for the daemon — the UI physically cannot persist a config the daemon would reject. On failure the daemon's own message is shown and the file is untouched.
- **Round-trip write preserves the file.** A valid save rewrites `config.yaml` in place, preserving `${VAR}` placeholders, key order, and operator comments (via a round-trip serializer such as `ruamel.yaml`, added to the `ui` extra only — the daemon keeps reading with `yaml.safe_load`, which parses round-trip output fine). Write is atomic (temp file + rename) so a crashed write never leaves a truncated config the daemon might reload.
- **Reload via daemon file-watch, replacing the broken `SIGHUP`.** The daemon adds an async watch task that detects a content change to the config file and calls the existing reload routine (`_on_sighup`'s body, extracted to `_reload_config`). The config bind-mount changes from a **single file** to the **containing directory**, so an atomic rewrite's new inode is observed (the root cause of the current staleness). `SIGHUP` stays wired as a manual belt-and-suspenders. No dependency is required if the watch polls a cheap content hash on the scan interval; `watchfiles` is an option if inotify latency matters.
- **Startup-only fields are labeled, not faked.** Fields consumed only at startup (`controllers[]` host/creds/port, `home_assistant` url/token, `dns_sources[]` wiring) are `restart_required: true` in the schema. Saving one shows a "saved — restart the daemon to apply" notice; live-reloadable fields show "applied on next scan cycle." The UI never claims a live effect a reload can't deliver.
- **Env cleanup.** `UNIFI_HOST/USERNAME/SITE/PORT` become plain literals in `config.yaml`, editable in the UI, and are removed from `env.example` and the live env file. `UNIFI_PASSWORD`, `HA_TOKEN`, `PIHOLE_PASSWORD` remain env vars (referenced as `${...}`). The parallel `WIFI_SHEPARD_UI_ALLOWLIST` env is **removed** — now that the UI reads `config.yaml`, it reads the authoritative `allowlist:` from there, closing the documented two-places-in-sync gotcha.
- **Write-route auth amends ADR-0002's fence.** `_assert_no_write_routes` is amended to allow the settings mutation route(s) (an explicit allowlist, not a blanket lift — every *other* route stays GET-only). When `WIFI_SHEPARD_UI_TOKEN` is set, a save without a valid `Authorization: Bearer` token returns `401` and does not touch the file. The mutation carries the token in a **header** (via `fetch`/HTMX), not an ambient cookie, so a cross-site form POST cannot forge a write. The startup WARN emitted when no token is set is upgraded to note that settings are now editable — writes follow the same optional-token posture as reads (homelab default), with a documented recommendation to set the token once write paths exist.
- **Scope is the whole surface, built in phases.** The decision is "everything editable," but the implementation lands scalars/enums first, then nullable-to-disable and scalar lists, then the object-list editors (`overrides[]`, `controllers[]`, `dns_sources[].instances[]`), so each phase is shippable.

## Acceptance Criteria

- [x] **AC-1**: A declarative settings schema enumerates every editable config field with its YAML path, kind, default, constraints, `secret` flag, `restart_required` flag, and a plain-English description; a test asserts every field reachable from the daemon's `Config` dataclasses is either present in the schema or on an explicit exclusion list, so no setting is silently un-editable.
- [x] **AC-2**: `GET /settings` renders the **current** `config.yaml` — scalars, enums, nullable-to-disable fields, scalar lists, and object lists (`overrides[]`, `controllers[]`, `dns_sources[]` + `instances[]`) — pre-filled from the file on disk, so an out-of-band hand edit to `config.yaml` is reflected on the next load (UI is a view over the file).
- [x] **AC-3**: For every secret field (controller `password`, HA `token`, DNS-source `password`), the form shows/accepts an env var **name** and round-trips it as a `${NAME}` placeholder; a rendered `/settings` response contains no interpolated secret value, and the UI never reads, stores, or logs the resolved secret.
- [x] **AC-4**: On save the UI validates the proposed config with the daemon's own `build_config` (secrets as literal `${NAME}` placeholders, no interpolation); an invalid value (out-of-range threshold, malformed MAC, unknown enum, all-null detection trio) is rejected with the daemon's error message and `config.yaml` on disk is left byte-for-byte unchanged.
- [x] **AC-5**: A valid save writes `config.yaml` atomically and in place, preserving `${VAR}` placeholders, key order, and operator comments; re-reading the file yields the saved values and the daemon's `load_config_from_path` parses it without error.
- [x] **AC-6**: The daemon detects a content change to its config file and reloads through the existing `update_config` path within one watch interval — no `SIGHUP`, no restart; a live-reloadable change (e.g. a detection threshold) takes effect on the next scan cycle. The config mount is the containing **directory** so an atomic rewrite's new inode is observed.
- [x] **AC-7**: Startup-only fields (`controllers[]` host/creds/port, `home_assistant` url/token, `dns_sources[]` wiring) are marked `restart_required` in the schema; saving one shows a "restart to apply" notice while a live-reloadable field shows "applied on next cycle" — the UI never claims a live effect a reload can't deliver.
- [x] **AC-8**: `UNIFI_HOST/USERNAME/SITE/PORT` are plain literals in `config.yaml`, editable in the UI, and removed from `env.example`; only `UNIFI_PASSWORD`/`HA_TOKEN`/`PIHOLE_PASSWORD` remain as env vars; the parallel `WIFI_SHEPARD_UI_ALLOWLIST` env is removed and the UI reads the authoritative `allowlist:` from `config.yaml`.
- [x] **AC-9**: The ADR-0002 read-only fence is amended to permit only the settings mutation route(s) (all other routes stay GET-only, verified by a test); when `WIFI_SHEPARD_UI_TOKEN` is set, a save without a valid bearer token returns `401` and does not modify the file; the mutation carries the token in a header, not a cookie, so a cross-site POST cannot forge a write.
- [x] **AC-10**: With the UI unused, the daemon behaves exactly as before (`config.yaml` unchanged, existing daemon + UI suites green); a fresh deploy with no `config.yaml` renders a settings page seeded from defaults instead of returning 5xx.

## Consequences

### Positive

- Every knob becomes editable from one page with a plain-English explanation — the detection thresholds stop being folklore, and tuning no longer means hand-editing YAML and recreating a container.
- Secrets never enter the UI, the browser, or the DB — the `${VAR}` placeholder keeps resolution in the daemon's environment, matching the Frigate model the operator asked for.
- The reload path is repaired: a directory mount + content-watch fixes the inode staleness and the un-forwarded `SIGHUP` in one move, so saved changes actually take effect.
- One source of truth (`config.yaml`), reused validation (`build_config`), and the removal of the parallel `WIFI_SHEPARD_UI_ALLOWLIST` env close two standing sync hazards.
- Failure isolation and the daemon's single-purpose design are preserved — the write path lives in the sidecar, not the kicker.

### Negative

- The sidecar is no longer read-only — a real reversal of ADR-0002. It gains a writable config mount and mutation routes, so its auth story matters more (previously "read-only by construction" meant nothing to protect).
- Round-tripping a comment-rich YAML pulls in a round-trip serializer (`ruamel.yaml`) or accepts comment loss — a small new dependency/decision in the `ui` extra.
- A full editor for the entire nested surface (per-MAC overrides, controllers, dns_sources + instances) is substantial UI work, phased across multiple PRs.
- A new daemon watch task, however small, is added surface on the process whose stability is paramount.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| A bad save writes a config the daemon then crash-reloads on | Low | High | Validate with `build_config` **before** writing (AC-4); atomic temp-file+rename write; daemon `_reload_config` already catches parse errors, logs `config_reload_failed`, and keeps the last-good config |
| Comment-rich live `config.yaml` mangled/reordered on rewrite | Medium | Low | Round-trip serializer preserves comments + key order (AC-5); tracked `config.example.yaml` is never rewritten |
| Write route exposed without a token, config editable by anyone on the network | Medium | Medium | Header-carried bearer token (CSRF-safe); upgraded startup WARN; README/`env.example` document setting the token before exposing writes; compose still `expose:` not `ports:` |
| A secret value leaks into the UI/DB/logs | Low | High | UI never interpolates; stores/renders only `${NAME}`; `build_config`'s `repr=False`/`_redact` path already masks secrets in errors (AC-3) |
| Watcher misses a change / busy-loops | Low | Medium | Poll a cheap content hash on the scan interval (bounded), or `watchfiles`; `SIGHUP` stays as a manual fallback |
| Operator edits a `restart_required` field and expects it live | Medium | Low | Schema flag + explicit "restart to apply" notice per field (AC-7) |
| Editing the file out-of-band races a UI save | Low | Low | UI reads-then-writes the whole file; last-write-wins with the file as source of truth; a compare-on-save guard can warn if the file changed under the editor |

## Implementation Plan

Phased so each lands shippable and testable.

- [x] **Phase 0 — reload + mount fix (daemon).** Extract `_on_sighup`'s body to `_reload_config`; add an async config-watch task (content-hash poll on the scan interval) that calls it. Change the compose config mount from single-file to directory. Tests: a rewritten file triggers a reload within one interval; a parse-failing rewrite keeps the last-good config.
- [x] **Phase 1 — settings schema + descriptions.** New schema module enumerating the full `Config` surface with kinds/constraints/`secret`/`restart_required`/description. Test that every `Config` field is covered or explicitly excluded (AC-1). This is where the plain-English threshold copy is written and reviewed.
- [x] **Phase 2 — read model + `GET /settings`.** UI reads `config.yaml` into the schema, renders scalars/enums/nullable/scalar-lists pre-filled; secret fields show the env-var **name** (AC-2, AC-3). Empty-state seeds from defaults (AC-10).
- [x] **Phase 3 — validate + write (round-trip).** Assemble mapping → `build_config` validate (AC-4) → atomic round-trip write preserving comments/placeholders (AC-5). Amend `_assert_no_write_routes` to allow the mutation route only; header-token auth on it (AC-9).
- [x] **Phase 4 — object-list editors.** `overrides[]`, `controllers[]`, `dns_sources[]` + `instances[]`, reboot `eligible[]`/`overrides[]`. `restart_required` notices wired (AC-7).
- [x] **Phase 5 — env cleanup + docs.** Move `UNIFI_HOST/USERNAME/SITE/PORT` to literals; strip them + `WIFI_SHEPARD_UI_ALLOWLIST` from `env.example`; update `config.example.yaml`, `README.md`, `CLAUDE.md`, and the deployment memory (AC-8). Compose fragment: directory mount + `:rw` for the UI.
- [ ] **Phase 6 — graduation.** Recreate both containers with the new mounts; verify reload works end-to-end in the live home stack.

## Related ADRs

- [ADR-0002](./0002-device-history-and-status-ui.md) — the read-only UI sidecar this **amends**; its "read-only in v1 / AC-6 no-write-routes" decision is the follow-up ("UI write paths / threshold-tuning UI") it explicitly deferred. ADR-0002 stays Implemented for its read views; only its write-path prohibition is lifted here.
- [ADR-0001](./0001-mvp-scope-base-feature.md) — the config schema, `${VAR}` interpolation, and fail-closed validation this reuses; the "no web framework in the daemon" constraint that rules out Option 2.
- [ADR-0009](./0009-disable-able-detection-criteria.md) — the `null`-disables-a-criterion convention the schema must express as a first-class "disable this signal" toggle.
- [ADR-0007](./0007-action-policy-backoff-and-quiet-hours.md) — quiet-hours + per-MAC backoff/override fields the editor must round-trip, including the `override > global` resolution surfaced as per-MAC forms.

## References

- `src/wifi_shepard/config.py` (`build_config`, `load_config_from_path`, `_interpolate_env`, `_SECRET_KEYS`/`_redact`), `main.py` (`_on_sighup`, startup-only wiring), `wifi_shepard_ui/app.py` (`_assert_no_write_routes`, bearer-token middleware).
- Deployment record: broken-`SIGHUP` root cause (uv PID 1 + single-file inode pin), gitignored live `config.yaml`, `WIFI_SHEPARD_UI_ALLOWLIST` parallel-env gotcha.
- Frigate config editor + `{FRIGATE_*}` env substitution — prior art for "env in the YAML, UI syncs with the YAML."
- `PLAN.md` §7 (config schema), §5 (stack / "no UI in the daemon").
