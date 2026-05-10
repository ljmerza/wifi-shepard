# ADR-0003: Kick Mechanism Upgrade — Speculative 802.11v BTM with One-Cycle Deauth Fallback

**Status:** Accepted
**Date:** 2026-05-09
**Author:** Leonardo Merza

## Context

### Background

[ADR-0001](./0001-mvp-scope-base-feature.md) shipped the daemon with a single kick mechanism — deauth via `Controller.force_reconnect_client(mac)` — and explicitly deferred 802.11v BTM (BSS Transition Management) "to a future ADR" while keeping `send_btm_request(mac, target_bssid)` declared on the Protocol so a future BTM-capable backend slots in without re-shaping it. This is that future ADR.

Deauth works on every client (the AP just stops talking to it), but it is the bluntest possible mechanism: the client loses its association, has to re-scan from scratch, re-auth, re-associate, re-DHCP. On a capable client, sending an 802.11v BSS Transition Management request lets the **client** choose a new AP from a candidate list while keeping its layer-2 association alive — orders of magnitude gentler. A misbehaving 2.4 GHz IoT device that supports BTM should never need to be deauth'd; one that doesn't, must.

### Current State

- **Protocol** (`src/wifi_shepard/controllers/base.py:52`) already declares `send_btm_request(mac: str, target_bssid: str | None = None) -> None` as optional. Backends without BTM raise `NotImplementedError`.
- **UniFi backend** (`src/wifi_shepard/controllers/unifi.py:163`) currently raises `NotImplementedError("UniFi backend does not implement BTM in MVP (ADR-0001)")`. Test coverage at `tests/test_unifi_controller.py:352`.
- **Actor** (`src/wifi_shepard/actor.py:49`) unconditionally calls `await self.controller.force_reconnect_client(mac)`. There is no mechanism selector and no fallback path.
- **`aiounifi` exposes NO BTM call** — verified: only `cmd: kick-sta` (deauth) via `ClientReconnectRequest` at `aiounifi/models/client.py:140`. UniFi's controller does support BTM via raw REST (`/api/s/<site>/cmd/devmgr` with a `bss-transition`-shaped payload), but that path is undocumented and untyped.
- **No usable BTM-capability discriminator exists on UniFi's wire format.** Empirically verified against a live UDM Pro with 77 wireless clients (5 sampled): the only `is_*` capability field is `is_11r` (Fast BSS Transition / 802.11r). There is no `is_11v`, no `is_11k`, no `wnm`, no `btm`, no `bss_transition`, no `extended_capabilities`. `is_11r` is `False` even for clients (e.g. Pixel 6a) that support it in hardware, because reporting depends on the WLAN's Fast Roaming setting — meaning `is_11r` is a property of network configuration, not device capability. **Auto-mode cannot rely on a capability flag.**
- **Schema**: `kick_events` (`src/wifi_shepard/db.py:24`) has columns `id, ts, mac, dry_run` only — no `mechanism` column. The UI sidecar reads this table via `src/wifi_shepard_ui/views.py`; any schema change is a coordinated bump per [ADR-0002 §Risks](./0002-device-history-and-status-ui.md#risks--mitigations).

### Requirements

1. The actor must be able to choose between deauth and BTM **per kick attempt**, with the choice driven by a new `kick_mechanism` config knob and the existing per-MAC `overrides:` mechanism (resolution: override > global, matching [ADR-0001 AC-6](./0001-mvp-scope-base-feature.md#acceptance-criteria)).
2. A new `auto` value must speculatively try BTM first on every kick candidate, then fall back to deauth if the client did not roam within one poll cycle. No per-client capability detection is performed; the daemon discovers BTM-capability per-kick by observation.
3. When BTM is sent and the client does not roam within one poll cycle, the actor must fall back to deauth — recording both attempts as one logical "kick group" linked by an `attempt_group` UUID, and counting the pair as **one** logical kick against the per-day / per-hour budget (not two).
4. The default value for `kick_mechanism` must be `deauth`, preserving MVP behavior bit-for-bit unless the operator opts in.
5. The `kick_events` schema must record which mechanism fired, what target BSSID (if any) was specified, and an `attempt_group` identifier linking BTM-then-deauth fallback rows together.
6. The UI sidecar's device-history view must surface the mechanism column without breaking ADR-0002 AC-3 (chronological timeline, dry-run rows visually distinguished).
7. Existing deployments must upgrade without data loss — old `kick_events` rows backfilled with `mechanism='deauth'`.
8. The dry-run code path (`scanner.dry_run: true`) must continue to log `would_kick` without calling either `force_reconnect_client` or `send_btm_request`. Mechanism choice is logged in the structured event so operators can audit it during the dry-run validation period.

### Constraints

- One Python process, one event loop ([ADR-0001 §Constraints](./0001-mvp-scope-base-feature.md#constraints)). The fallback path cannot use a blocking sleep — it must wait by yielding to the next scan cycle.
- The Protocol shape is locked by [ADR-0001 §Decision](./0001-mvp-scope-base-feature.md#decision) — `send_btm_request` is already declared. No Protocol additions in this ADR; only implementation changes in `UniFiController` and the actor.
- Schema changes coordinate with the UI sidecar (`src/wifi_shepard_ui/views.py`) per [ADR-0002 §Negative](./0002-device-history-and-status-ui.md#negative).
- Don't push vendor specifics into the scorer (CLAUDE.md). Mechanism selection lives in the actor and the controller, not in `scorer.py`.
- Secrets handling unchanged: the raw-REST BTM call uses the same UniFi session/cookies as the deauth call.
- `aiounifi` is pinned at `==85` (`pyproject.toml:10`); the raw-REST BTM call must work against UniFi controllers compatible with that pin. Schema drift on the BTM endpoint is a fail-closed condition matching the existing `_require()` posture.

## Options Considered

### Option 1 (original): Hybrid auto with capability detection — rejected

**Description:** `kick_mechanism: deauth | btm | auto` with per-MAC override. In `auto`, dispatch BTM to clients reporting `is_11v: true`, deauth to the rest. Per-MAC override wins.

**Rejected because:** The capability discriminator (`is_11v`) does not exist on UniFi's wire format. Empirical probe against a live UDM Pro confirmed only `is_11r` is exposed, and that field reports network configuration (Fast Roaming on/off) rather than device capability. The whole design depended on a flag that isn't there.

### Option 2: Explicit-mechanism-only (no auto)

**Description:** `kick_mechanism: deauth | btm` (no `auto`). Operator declares per-MAC mechanism in `overrides:`. No capability detection. Same UniFi raw REST + schema additions.

**Pros:**
- Simplest mental model — what's in YAML is exactly what runs.
- No false-positive risk from a missing or unreliable capability flag.
- No fallback path — one mechanism per attempt, smaller test surface.

**Cons:**
- Requires operators to maintain a per-MAC mechanism table for ~30+ IoT clients. Most homelab operators won't audit every device's 802.11v support, so the default (`deauth`) becomes the only mechanism that ever runs in practice.
- Loses the "send BTM when capable, deauth when not" value-prop without offering an alternative path to discover capability.
- BTM-targeted at an incapable client is a no-op silent failure — the daemon sends the request, the client ignores it, and the next poll re-fires. Without auto-fallback, this becomes an infinite no-op until kick budget exhaustion.

### Option 3 (Chosen): Speculative BTM with one-cycle deauth fallback

**Description:** `kick_mechanism: deauth | btm | auto` with per-MAC override (default `deauth`). In `auto`, send BTM to every kick candidate first; on the next poll cycle, if the client is still bad-state on the same `ap_id`, fall back to deauth under the same `attempt_group` UUID. No capability detection — the daemon learns BTM-capability per-kick by observation. The pair counts as one logical kick against the per-day / per-hour budget.

**Pros:**
- No discriminator needed — works against UniFi's wire format as it actually exists.
- Capable clients (most modern phones, laptops, recent TV/streaming devices) get the gentle BTM nudge and roam without ever being deauth'd.
- Incapable clients converge to deauth automatically without operator effort, with a one-cycle observability cost.
- The `attempt_group` UUID lets the UI surface a real story ("tried BTM at T, fell back to deauth at T+60s") and lets the backoff budget treat the pair as one kick.
- Per-MAC `overrides[].kick_mechanism: deauth` lets operators pin known-incapable clients (e.g. ESP8266 WLEDs) directly to deauth, skipping the wasted 60s once they're identified.
- Default `deauth` preserves MVP behavior — operators who do nothing see zero change.

**Cons:**
- BTM-incapable clients pay one extra poll cycle (60s default) of bad airtime per kick before fallback fires. On heavily-IoT networks this is a real but bounded cost.
- Two `kick_events` rows per kick on incapable clients (one BTM no-op + one deauth fallback). The `attempt_group` UUID groups them in the UI, but the table grows faster.
- BTM is implemented via raw REST against UniFi's undocumented `/api/s/<site>/cmd/devmgr` BTM payload — no `aiounifi` typed call exists. A controller upgrade can break the call.
- The "did the client roam?" check reads `client_samples.ap_id` retroactively, coupling the actor (slightly) to the scanner's poll interval.

### Option 4: Use `is_11r` as a soft proxy

**Description:** `kick_mechanism: auto` dispatches BTM to clients with `is_11r=True`, deauth otherwise. `is_11r` is the only capability-shaped field UniFi actually exposes.

**Rejected because:** Empirically, `is_11r` reports the WLAN's Fast Roaming configuration, not the device's hardware capability. On a network with Fast Roaming disabled (the user's current state), every client reports `is_11r=False` regardless of capability — making `auto` indistinguishable from deauth-only. Enabling Fast Roaming to unblock this discriminator is a separate, larger network-config decision (some IoT clients don't handle 11r well) that shouldn't be coupled to a kick-mechanism decision.

### Option 5: Defer UniFi BTM — second backend first

**Description:** Don't touch UniFi BTM. Add Omada or OpenWRT backend where BTM is natively exposed by the upstream library.

**Rejected because:** The user's homelab is UniFi-only. Doing this delivers nothing for the actual hardware in play, and ADR-0001's *Anticipated follow-ups* explicitly listed second-backend work as its own ADR.

## Decision

**Chosen Option:** Option 3 — Speculative BTM with one-cycle deauth fallback.

**Rationale:**

1. The empirical probe killed Option 1's discriminator. With no reliable capability flag, the design must either (a) require operator effort per-device (Option 2), (b) defer BTM entirely (Option 5), or (c) discover capability by observation (Option 3). Option 3 is the only one that ships BTM by default for capable clients without operator effort.
2. The cost of Option 3's "wasted 60s poll cycle on incapable clients" is real but bounded by the existing poll-interval default. The user's quarantine cap (5 kicks → permanent quarantine, ADR-0001 §4) means the absolute worst case is ~5 wasted minutes per defective device before it's flagged out of the kick rotation entirely.
3. Per-MAC `overrides[].kick_mechanism: deauth` is the operator escape hatch: once an incapable client is identified (the `kick_no_roam` log line and the doubled `kick_events` rows make this obvious), the operator pins it to deauth-only via a one-line YAML edit + SIGHUP. This keeps Option 3's worst case from ever becoming chronic.
4. The Protocol shape is **already in place** — Option 3 implements `send_btm_request` (currently `NotImplementedError`) and the actor's mechanism selector. No Protocol changes.
5. Per-MAC `overrides:` resolution mirrors the pattern already trained into operators by [ADR-0001 AC-6](./0001-mvp-scope-base-feature.md#acceptance-criteria) (`tests/test_threshold_resolution_ac6.py`). Threading `kick_mechanism` through it adds **zero new operator concepts**.
6. Default `deauth` preserves the dry-run-period guarantees of [ADR-0001 §Negative](./0001-mvp-scope-base-feature.md#negative). An operator who upgrades and does nothing else sees identical behavior.
7. The raw-REST risk against UniFi's `cmd/devmgr` BTM endpoint is real but bounded: the call only fires when the operator opts in (`kick_mechanism: btm` or `auto`), and a `_require`-style schema-validation guard makes a controller-version drift fail closed (matching ADR-0001's stance on `aiounifi` API drift).

**Implementation forks resolved by this ADR:**

- **Capability detection (Fork A):** None. Capability is discovered per-kick by observation (did the client roam after BTM?). Per-MAC override (`overrides[].kick_mechanism: deauth`) lets operators pin known-incapable clients to deauth-only after observing one wasted-cycle confirmation.
- **Default mechanism (Fork B):** Config knob `kick_mechanism: deauth | btm | auto`, default `deauth`. The `auto` value triggers speculative-BTM-then-deauth-fallback on every kick.
- **Target BSSID (Fork C):** `None` — let the client pick from its own neighbor scan. The Protocol already defaults to this.
- **UniFi BTM impl (Fork D):** Raw REST against `/api/s/<site>/cmd/devmgr`. Payload shape pinned to a recorded fixture; schema-drift fails closed.
- **Schema (Fork E):** Add `mechanism TEXT NOT NULL DEFAULT 'deauth'`, `target_bssid TEXT`, and `attempt_group TEXT` columns to `kick_events`. Forward-compatible migration: `ALTER TABLE` on connect for existing DBs.
- **Effectiveness (Fork F):** Compare `client_samples.ap_id` at the kick row's `ts - 1 * poll_interval` vs `ts + 1 * poll_interval`. Logged as a structured `kick_succeeded` / `kick_no_roam` event, not stored. Outcomes table deferred.
- **Fallback timing (Fork G):** Wait one full poll cycle (G2). The next scan cycle's score result triggers fallback if the client is still bad-state with the same `ap_id`. No `asyncio.sleep` in the actor.
- **Kick-budget accounting:** One logical kick per `attempt_group`. The backoff state machine increments at the BTM stage; the deauth fallback rolls under the same group without re-incrementing.
- **802.11k assist (Fork H):** Out of scope. Documented as a candidate for a future additive ADR.

## Acceptance Criteria

- [ ] **AC-1**: Given `kick_mechanism: deauth` (the default) and a bad-state client, when the actor handles it with `dry_run: false`, then `Controller.force_reconnect_client(mac)` is called exactly once, `Controller.send_btm_request` is **never** called, and the resulting `kick_events` row has `mechanism='deauth'`.
- [ ] **AC-2**: Given `kick_mechanism: btm` (explicit) and a bad-state client, when the actor handles it with `dry_run: false`, then `Controller.send_btm_request(mac, target_bssid=None)` is called exactly once, `Controller.force_reconnect_client` is **never** called in this attempt, and the `kick_events` row records `mechanism='btm'`, `target_bssid IS NULL`, and a fresh `attempt_group` UUID.
- [ ] **AC-3**: Given `kick_mechanism: auto` and a bad-state client, when the actor handles it with `dry_run: false`, then `Controller.send_btm_request(mac, target_bssid=None)` is called first (no capability check), the `kick_events` row records `mechanism='btm'` with a fresh `attempt_group` UUID, and the per-MAC backoff budget is incremented by exactly one (not two).
- [ ] **AC-4**: Given a prior BTM attempt under `attempt_group=G` for MAC `M`, and the next scan cycle scores `M` still bad-state on the same `ap_id`, when the actor handles it, then `Controller.force_reconnect_client(M)` is called, a second `kick_events` row is written with `mechanism='deauth_fallback'` and the **same** `attempt_group=G`, and the per-MAC backoff budget is **not** incremented again (still one logical kick for the group).
- [ ] **AC-5**: Given a per-MAC `overrides[].kick_mechanism: deauth` and a global `kick_mechanism: btm`, when scoring resolves the mechanism for that MAC, then the resolved value is `deauth` and a different MAC's resolved value is `btm` — matching ADR-0001 AC-6 override-resolution semantics.
- [ ] **AC-6**: Given `client_samples` rows with `ap_id=X` at T−60s, a kick row at T, and a sample at T+60s with `ap_id=Y` (Y ≠ X), when the post-kick check fires on the next cycle, then a structured `kick_succeeded` log line is emitted with `from_ap=X, to_ap=Y, mechanism=<the mechanism>, attempt_group=<uuid>`. Same scenario but `ap_id=X` again → `kick_no_roam` log line with the same fields.
- [ ] **AC-7**: Given a fresh DB written under the new schema and `GET /devices/{mac}` from the UI sidecar, when the response renders, then each timeline row shows the mechanism (`deauth` / `btm` / `deauth_fallback`), and dry-run rows remain visually distinguished per ADR-0002 AC-3. Rows sharing an `attempt_group` are visually grouped.
- [ ] **AC-8**: Given a pre-existing `state.db` written under the ADR-0001 `kick_events` schema (no `mechanism` column), when the daemon connects on startup, then a forward-compatible migration runs once, adds the new columns with `mechanism DEFAULT 'deauth'` for backfilled rows, no rows are lost, and the UI sidecar still loads `/devices/{mac}` without 5xx.
- [ ] **AC-9**: Given `scanner.dry_run: true` and `kick_mechanism: auto`, when the actor handles a bad-state client, then the structured `would_kick` log line includes a `mechanism: 'btm'` field, and **neither** `force_reconnect_client` nor `send_btm_request` is called.

## Consequences

### Positive

- BTM-capable clients (modern phones, laptops, recent IoT) get a gentle layer-2 nudge instead of a full re-association — measurably lower airtime cost per kick.
- Operators who don't opt in see zero behavior difference. The default-`deauth` posture matches ADR-0001's "fail safe" stance.
- No capability-detection plumbing — the daemon doesn't depend on any wire-format field that may not exist.
- Per-MAC `kick_mechanism` override gives operators a precise tool: pin a flaky-BTM device to `deauth` after one observation, leave the rest on `auto`.
- Effectiveness logging (`kick_succeeded` / `kick_no_roam`) creates an evidence trail for tuning future ADRs (e.g. "what fraction of clients roam on BTM?") without committing to a metrics store.
- The `attempt_group` UUID lets the UI surface a real story and lets the backoff budget treat one BTM+deauth pair as one logical kick.

### Negative

- BTM-incapable clients pay one extra poll cycle (60s default) of bad airtime per kick before fallback fires. Bounded by the operator's escape hatch (per-MAC override → deauth-only) once an incapable client is identified.
- Doubled `kick_events` rows for incapable clients during the discovery period. UI groups by `attempt_group`; storage cost is small at the existing per-day kick caps.
- Raw REST against UniFi's undocumented `cmd/devmgr` BTM endpoint. A controller upgrade can break the call; recovery requires updating a fixture and re-pinning.
- Schema migration is a coordinated bump with the UI sidecar — both images need a release together. ADR-0002 §Risks already flagged this class of issue.
- Effectiveness measurement reads `client_samples` retroactively, which couples the actor (slightly) to the scanner's poll interval.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| UniFi controller upgrade breaks raw-REST `cmd/devmgr` BTM payload | Medium | High | Pin payload shape against a recorded fixture (mirroring [ADR-0001 §Mitigations](./0001-mvp-scope-base-feature.md#risks--mitigations) for `aiounifi` drift). Fail closed on non-2xx; emit a clear `btm_endpoint_failed` log; the actor's deauth fallback path keeps kicks working. |
| Wasted-cycle cost on BTM-incapable clients chronic on IoT-heavy networks | Medium | Medium | Operator escape hatch: per-MAC `overrides[].kick_mechanism: deauth` after one observation. The `kick_no_roam` log line and doubled `kick_events` rows make incapable clients self-identifying. README / `config.example.yaml` document the workflow. |
| Backoff budget double-counts BTM+deauth pair as two kicks | Medium | High | The state machine MUST increment `record_kick(mac)` once per `attempt_group`, not once per `kick_events` row. AC-3 / AC-4 directly test this invariant. |
| BTM-attempt-group state leaks (daemon restart loses in-flight attempt_groups) | Low | Low | Persist `attempt_group` and the timestamp of the BTM attempt in `mac_state` (or a small `pending_btm` table). On restart, the next-cycle fallback path rehydrates from disk. Tests AC-4 cover the cold-start case implicitly via the "next scan cycle reads from DB" path. |
| Schema migration races a UI sidecar startup | Low | Medium | Migration runs in `Database.connect()` before the daemon serves any other request. UI sidecar opens DB read-only; if the migration hasn't run yet, the UI's startup smoke-test (ADR-0002 §Risks) catches the missing column and fails fast with a clear log line. |
| Operator flips `kick_mechanism: auto` before validating BTM works on their network | Medium | Medium | Default stays `deauth`. README / `config.example.yaml` recommend a ≥1-week dry-run-period observation with `kick_mechanism: auto` + `dry_run: true` so the operator sees `would_kick mechanism=btm` lines before any real BTM is sent. |
| Phase 8 integration step finds the raw-REST BTM payload shape is wrong | Medium | High | Phase 8 is allowed to invalidate Phase 2's implementation — the AC tests pass against a fixture, not a real controller. Treat the integration step as a discovery exercise, not a rubber-stamp. If the payload shape is wrong, fix `unifi.py` and the fixture; the AC tests remain valid because they only check that `send_btm_request` is called and the row is written, not the wire-level payload. |

## Implementation Plan

Build order optimized so each phase ends with something runnable and testable.

- [ ] **Phase 0 — schema & migration** (`src/wifi_shepard/db.py`): add `mechanism`, `target_bssid`, `attempt_group` columns with forward-compatible `ALTER TABLE` on connect. Update `insert_kick(...)` signature. Tests: `tests/test_db_migration_kick_events.py` (AC-8).
- [ ] **Phase 1 — config plumbing** (`src/wifi_shepard/config.py`): new `kick_mechanism` field on the global config block (`Literal["deauth", "btm", "auto"]`, default `"deauth"`); add the same field to the per-MAC override schema. Tests mirror `tests/test_threshold_resolution_ac6.py` shape: `tests/test_threshold_resolution_kick_mechanism.py` (AC-5).
- [ ] **Phase 2 — UniFi BTM call** (`src/wifi_shepard/controllers/unifi.py`): replace `send_btm_request`'s `NotImplementedError` with a raw REST POST against `/api/s/<site>/cmd/devmgr`. Reuse the existing `aiohttp.ClientSession` from `_session`. Replace `tests/test_unifi_controller.py:352`'s `NotImplementedError` test with new behavior tests; add `tests/test_unifi_btm.py` against a fixture response. **Note**: AC tests check that the call is made and the row is written, not the wire-level payload — Phase 8 integration may invalidate the fixture.
- [ ] **Phase 3 — actor mechanism selector** (`src/wifi_shepard/actor.py`): resolve mechanism from config (override > global), call BTM or deauth accordingly, pass `mechanism` and `attempt_group` to `db.insert_kick`. The `auto` path always sends BTM first (no capability check). Tests: `tests/test_actor_btm.py` (AC-1, AC-2, AC-3, AC-9).
- [ ] **Phase 4 — fallback path & budget accounting** (`src/wifi_shepard/actor.py` + new `pending_btm` state on `mac_state` or a small table): track in-flight BTM `attempt_group`s; on the next scan cycle, if the same MAC is still bad-state on the same `ap_id`, fire deauth under the same `attempt_group`. The backoff state machine's `record_kick` is called once per group. Tests: AC-4.
- [ ] **Phase 5 — effectiveness logging** (`src/wifi_shepard/scanner.py` or a small post-kick checker): on the cycle after a kick, compare pre/post `ap_id` and emit `kick_succeeded` / `kick_no_roam`. Tests: AC-6.
- [ ] **Phase 6 — UI sidecar surface** (`src/wifi_shepard_ui/views.py` + `templates/history.html`): add `mechanism` to the device-history query and template; visually group rows sharing an `attempt_group`. Tests: `tests/ui/test_history_mechanism_column.py` (AC-7). Update `views.py`'s smoke-test column-existence assertion (per ADR-0002 §Risks) to include the new columns.
- [ ] **Phase 7 — config docs**: update `config.example.yaml` to show `kick_mechanism: deauth | btm | auto` with a comment recommending ≥1-week observe period before flipping to `auto`. Update `CLAUDE.md`'s config-shape paragraph. Document the per-MAC override escape hatch for incapable clients.
- [ ] **Phase 8 — integration**: end-to-end smoke against the recorded UniFi fixture; manual verification on the UDM Pro with `kick_mechanism: auto` + `dry_run: true` for ≥1 week per AC-9 risk-table item. Then flip `dry_run: false` once the `would_kick mechanism=btm` lines look right. **This phase may invalidate the Phase 2 BTM payload fixture** — that's expected, not a regression.

## Related ADRs

- [ADR-0001](./0001-mvp-scope-base-feature.md) — defines the Protocol shape this ADR implements (`send_btm_request`), the `dry_run` gate this ADR honors, the override-resolution pattern this ADR mirrors (AC-5), and the per-day kick caps this ADR composes with.
- [ADR-0002](./0002-device-history-and-status-ui.md) — defines the read-only UI sidecar that surfaces `kick_events`. Phase 6 of this ADR's Implementation Plan adds the `mechanism` column to that surface.

Anticipated follow-ups (not yet written):

- **ADR for second `Controller` backend** (Omada / OpenWRT) — proves Protocol portability. ADR-0003's mechanism selector + per-MAC `kick_mechanism` resolution land first; the second backend implements BTM natively where the upstream library exposes it, and the actor's selector is already in place.
- **ADR for capability-aware kick selection** — if/when a future UniFi firmware exposes `is_11v` or similar, revisit auto-mode to skip BTM on known-incapable clients (saving the 60s wasted-cycle cost). Empirically this didn't exist on UDM Pro firmware as of 2026-05-09.
- **ADR for 802.11k neighbor-report assist** — additive: push neighbor reports to clients before BTM (or alongside it) to give them a richer roam-target list. Out of scope here per Fork H.
- **ADR for kick-outcome persistence** — promote effectiveness logging (AC-6) into a `kick_outcomes` table once the homelab has weeks of data and we know what queries we want.
- **ADR for ControllerSpec port field** — `src/wifi_shepard/config.py:86` `ControllerSpec` does not carry a `port` field, so the daemon defaults to UniFi standalone-controller port 8443 and cannot reach UDM/UDM Pro (port 443) without a code change. Independent gap surfaced during this ADR's empirical probe; needs its own small ADR/PR.
- **ADR for Prometheus `/metrics` exporter** — surfaces BTM-vs-deauth split, `kick_no_roam` rate, attempt-group sizes as time-series gauges. Anticipated by both ADR-0001 and ADR-0002.

## References

- [`PLAN.md`](../../PLAN.md) §3 (detection rules), §4 (backoff schedule), §6 (Controller Protocol), §12 (BTM-aware roaming brainstorm).
- [`CLAUDE.md`](../../CLAUDE.md) — "don't push vendor specifics into the scorer" rule.
- [IEEE 802.11v-2011](https://standards.ieee.org/ieee/802.11v/4032/) — BSS Transition Management Request frame definition.
- [Mist BSS Transition Management overview](https://www.mist.com/documentation/802-11v-bss-transition-management/) — vendor-neutral description of the BTM exchange the daemon will trigger.
- [`aiounifi` `ClientReconnectRequest`](https://github.com/Kane610/aiounifi/blob/master/aiounifi/models/client.py) — the only kick path the library exposes (deauth via `cmd: kick-sta`); BTM is implemented out-of-band in this ADR.
- [`pydantic-settings` Literal validation](https://docs.pydantic.dev/latest/concepts/types/#literal) — how the `kick_mechanism: deauth | btm | auto` enum is validated at YAML parse time.
- Empirical UniFi wire-format probe (this ADR, 2026-05-09): UDM Pro, 77 wireless clients, 5 sampled. Only `is_11r` exists as a capability flag; no `is_11v`, `is_11k`, `wnm`, `btm`, `bss_transition`, or `extended_capabilities` fields. `is_11r` was `False` for all sampled clients (including a Pixel 6a) because the network had Fast Roaming disabled.
