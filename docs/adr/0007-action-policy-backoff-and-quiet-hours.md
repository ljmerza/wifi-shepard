# ADR-0007: Complete the Action Policy — Per-MAC Backoff Schedule, Hard Caps, and Quiet Hours

**Status:** Accepted
**Date:** 2026-05-30
**Author:** Leonardo Merza

## Context

### Background

`PLAN.md` §4 ("Action policy and backoff") specifies a per-MAC escalating backoff, hard per-device kick caps, and quiet hours. [ADR-0001](./0001-mvp-scope-base-feature.md) committed to building this (Requirement 3, AC-3, AC-5, Phase 4–5), but the shipped MVP implemented only the **quarantine-after-N** slice of it. The rest of §4 is missing:

- `backoff.py` is a counter plus `quarantine_after_kicks`; there is no escalating cooldown and no per-device hourly/daily cap.
- `config.py` parses only `quarantine_after_kicks` from the `backoff:` block. `cooldowns_seconds`, `max_kicks_per_hour`, `max_kicks_per_day` are silently dropped.
- `quiet_hours` has **zero** references in `src/` — the daemon will kick at 03:00 the same as at 15:00.

This is the most correctness-critical gap in the codebase: without it the action policy does not match the spec the operator configured against, and a defective device's kick-loop is bounded only by quarantine (5 kicks) rather than by the intended 3/hour · 10/day caps with escalating cooldowns.

### Current State

- **`scorer.py`** — sliding-window bad-state detection; `is_bad_state()` checks radio/signal/tx_rate/retry. Allowlist short-circuit at `scorer.py:40` (ADR-0001 AC-4, works). Per-MAC threshold resolution via `resolution.resolve_thresholds` (override > global).
- **`actor.py`** — kick gate. Order: `dry_run` → quarantine check (`backoff.should_quarantine`) → BTM-pending deauth fallback → fresh-kick gate (`rate_limiter.can_kick`, ADR-0004 global + per-AP) → send + record. Uses `now_fn = time.monotonic`.
- **`backoff.py`** — `BackoffManager`: in-memory `kick_count` per MAC, `should_quarantine` = `kick_count >= quarantine_after_kicks`, plus a `quarantine_notified` dedup flag.
- **`rate_limit.py`** — ADR-0004 **global single-flight + per-AP** cap, in-memory, injected clock. Its own docstring cites *"per-MAC backoff and per-day kick caps"* as the **durable** floor under it.
- **`db.py`** — `kick_events(id, ts REAL, mac, dry_run, mechanism, target_bssid, attempt_group)`, WAL. `ts` is wall-clock `time.time()`. `dry_run` path in the actor returns *before* `insert_kick`, so today the table holds real kicks only. The `Store` Protocol exposes inserts but **no read** method.
- **`reboot/scheduler.py`** — already does tz-aware HH:MM scheduling; the pattern to mirror for quiet hours.

### Requirements

1. **Escalating per-MAC cooldown** — after a MAC's *n*-th kick, the next kick is allowed only once `cooldowns_seconds[idx]` has elapsed, escalating with the trailing run of recent kicks and clamping at the last entry (`[300, 1800, 7200, 43200, 86400]` → 5 m, 30 m, 2 h, 12 h, 24 h).
2. **Per-MAC hard caps** — `max_kicks_per_hour` (default 3) and `max_kicks_per_day` (default 10), each per-MAC-overridable.
3. **Quiet hours** — a `start`/`end`/`timezone` window during which a MAC is kicked only if it trips the stricter `override_threshold`.
4. **Restart- and SIGHUP-safe caps** — a crash-loop must not be able to bypass the daily cap (the exact defective-device risk ADR-0001 names).
5. **Compose, don't replace** — the existing quarantine (ADR-0001 AC-5) and the ADR-0004 rate limiter keep working; per-MAC backoff is an additional gate. `test_quarantine_ac5` must stay green.
6. **Fail closed** — unsupported config (notably `quiet_hours.override_threshold.ap_cu_total_min`, see below) is rejected at parse time, not silently ignored.

### Constraints

- One process, asyncio, injected clocks for tests (`time-machine` is already a dev dep). Match `config.py`'s fail-closed validation helpers.
- **Out of scope (noted, not touched):** the **AP-saturation gate** (`detection.ap_cu_total_min`, PLAN §3) is *also* unimplemented — `ClientSnapshot.ap_cu_total` is plumbed but `scorer.is_bad_state()` never checks it, and `DetectionConfig`/`resolution._THRESHOLD_FIELDS` omit it. That is a §3 detection bug unrelated to §4 action policy; it gets its own follow-up ADR. Because `quiet_hours.override_threshold` references `ap_cu_total_min`, this ADR must *fail closed* on that key rather than pretend to honor it.

## Options Considered

The structural fork is **where per-MAC backoff state lives** (cooldown timer, hourly/daily counts, escalation level). Everything else follows from it.

### Option 1: DB-derived from `kick_events` (Chosen)

**Description:** On each fresh-kick decision, query the existing timestamped `kick_events` table for this MAC's recent real kicks and derive cooldown eligibility, hourly/daily counts, and escalation level from the row timestamps. Adds one read method to the `Store` Protocol.

**Pros:**
- Restart- **and** SIGHUP-safe for free — the DB is the single source of truth, so no in-memory state to lose or hand-preserve across a reload.
- `kick_events` already records `ts` (wall clock) and `dry_run`; no new table or migration.
- Honors ADR-0004's stated contract: its in-memory limiter leans on these caps being the *durable* floor.
- Matches PLAN §5 ("SQLite … used for kick history **and backoff timers**"). The decision function stays pure (timestamps + config in → decision out), mirroring `rate_limit.py` and `scorer.is_bad_state`.

**Cons:**
- One async `SELECT` per fresh-kick decision (home-lab scale: a handful of MACs, kicks bounded by the caps — negligible).
- Wants a `(mac, ts)` index eventually (not at this scale).

### Option 2: In-memory in `BackoffManager`

**Description:** Track per-MAC last-kick time and hourly/daily counts in memory with an injected clock, exactly like `rate_limit.py`.

**Pros:**
- Simplest; mirrors the existing rate limiter; no DB read on the hot path; trivial to unit-test.

**Cons:**
- A restart or crash-loop **resets the daily cap** → bypasses 10/day, the precise kick-loop ADR-0001 guards against.
- Leaves ADR-0004's "durable floor" empty — undercuts the safety guarantee that motivates this work.
- SIGHUP reload must preserve in-flight state by hand.

### Option 3: Dedicated `mac_state` table

**Description:** A new persisted table, one row per MAC (`last_kick_at`, `kick_count`, …), loaded at startup and written on each kick.

**Pros:**
- Restart-safe; explicit O(1) lookups; no scan of `kick_events`.

**Cons:**
- New table + migration + write path, when `kick_events` is **already** the kick ledger — two sources of truth for "kicks" to keep in sync.
- The most code for the least marginal gain at this scale.

## Decision

**Chosen Option:** Option 1 — DB-derived from `kick_events`.

**Rationale:**

1. Requirement 4 (restart-safe caps) is the whole safety point, and only Options 1 and 3 satisfy it. Between them, `kick_events` is already the timestamped kick ledger, so Option 1 needs **one read method** and no migration, while Option 3 adds a second source of truth to keep in sync.
2. ADR-0004 explicitly designed its in-memory limiter *around* these caps being durable. Option 2 would leave that floor empty — a regression disguised as consistency.
3. A pure decision function over timestamps matches the codebase's established testable-core style (`rate_limit.py`, `scorer.is_bad_state`).

**Implementation forks resolved by this ADR:**

- **Two clocks in the actor.** The ADR-0004 rate limiter stays on `time.monotonic`. Per-MAC backoff compares against `kick_events.ts` (wall clock), so the actor gains a `wall_now_fn = time.time`. Quiet hours needs tz-aware wall time and is evaluated in the scorer (mirrors `reboot/scheduler.py`).
- **Escalation level = trailing run of recent kicks.** The cooldown index is `min(R-1, len-1)` where `R` = this MAC's real kicks within the **recovery window** (= `max(cooldowns_seconds)`). After the recovery window of quiet, the run empties and escalation resets to the first rung. Derived entirely from `kick_events` — no clean-cycle plumbing into `backoff.py`. *(Alternative considered: explicit clean-cycle feedback from the pipeline — more wiring, no benefit here.)*
- **Quarantine stays in-memory and unchanged.** Cooldowns/caps layer *beneath* it; `quarantine_after_kicks` and `should_quarantine` are untouched (`test_quarantine_ac5` stays green). The "device may be defective" signal is the **existing** quarantine notification (ADR-0001 AC-5), not a new event. Consequence: a restart resets the quarantine *count* (the durable daily cap still bounds the device); the `quarantine_notified` dedup flag staying in memory means at worst one duplicate quarantine notice after a restart.
- **Quiet-hours precedence: per-field "more-conservative-wins."** During the window, the effective threshold per field is the more-conservative of (per-MAC-resolved) and (`override_threshold`): `tx_rate_kbps_max → min`, `retry_pct_max → max`, `signal_dbm_max → min` (more-negative). Quiet hours therefore never *loosens* a per-MAC override. Fields absent from `override_threshold` keep their resolved value.
- **AP-saturation gate fails closed, not silently.** `quiet_hours.override_threshold` accepts only `tx_rate_kbps_max` / `retry_pct_max` / `signal_dbm_max`. `ap_cu_total_min` (and any unknown key) is rejected at parse time with a message pointing to the follow-up §3 AP-gate ADR.
- **Caps default ON.** `max_kicks_per_hour: 3`, `max_kicks_per_day: 10`, `cooldowns_seconds: [300,1800,7200,43200,86400]` are the defaults (PLAN §4 treats them as core safety; `0`/`[]` disables). `dry_run` (default true) still gates all real action, so the conservative defaults only bite once the operator opts into live kicks.

## Acceptance Criteria

- [ ] **AC-1**: Given `dry_run: false` and a MAC kicked once at T0, when its window is bad-state again at T0+Δ with Δ < `cooldowns_seconds[0]`, then no kick fires and a `kick_deferred` (reason `per_mac_cooldown`) is logged; at Δ ≥ `cooldowns_seconds[0]` the kick fires.
- [ ] **AC-2**: Given a MAC with N prior real kicks inside the recovery window, the cooldown enforced before the next kick is `cooldowns_seconds[min(N-1, len-1)]` — it escalates 300→1800→7200→43200→86400 and clamps at the last entry for N beyond the array length.
- [ ] **AC-3**: Given `max_kicks_per_hour: 3` and 3 real kicks for a MAC within the last 3600 s, when a 4th bad-state is detected, then it is deferred (reason `per_mac_hourly_cap`) until the oldest of the three ages past the hour.
- [ ] **AC-4**: Given `max_kicks_per_day: 10` and 10 real `kick_events` rows for a MAC within the last 86400 s, when an 11th bad-state is detected, then no kick fires — and because the count is read from `kick_events`, the cap holds after a process restart (in-memory state reset).
- [ ] **AC-5**: Given an `overrides:` entry `max_kicks_per_day: 3` for MAC X, then X is capped at 3 kicks/day while other MACs use the global default (10).
- [ ] **AC-6**: Given a `quiet_hours` window active at the current time, when a MAC meets the normal thresholds but not the stricter `override_threshold`, then it is not kicked; when it meets the stricter `override_threshold`, it is kicked. Outside the window the normal thresholds apply.
- [ ] **AC-7**: Given a config whose `quiet_hours.override_threshold` contains `ap_cu_total_min` (or any unsupported key), or whose `quiet_hours.timezone` is not a valid IANA zone, then `load_config` raises a clear `ValueError` at parse time.
- [ ] **AC-8**: Given the per-MAC backoff gate, the existing quarantine (ADR-0001 AC-5: `kick_count ≥ quarantine_after_kicks` → QUARANTINE + one notification) and the ADR-0004 global/per-AP rate limiter still apply; the BTM→deauth fallback under an existing `attempt_group` is NOT re-gated by the per-MAC cooldown/caps (it is the same logical kick).

## Consequences

### Positive

- The action policy finally matches `PLAN.md` §4 and the config operators already write against; the daily cap is durable across restarts.
- No schema migration — reuses the `kick_events` ledger and adds a single read method to the `Store` Protocol.
- Quiet hours and the cooldown/caps logic are pure functions over (timestamps, wall-clock, config), so they unit-test without a live controller or DB.

### Negative

- One `SELECT` per fresh-kick decision (negligible at home-lab scale; an index is the escape hatch if it ever isn't).
- Quarantine *count* and the `quarantine_notified` flag remain in memory, so a restart re-evaluates a quarantined device and may emit one duplicate quarantine notice. The durable daily cap bounds the blast radius.
- Caps defaulting ON makes a previously-unspecified `backoff:` block more conservative on upgrade — intended (PLAN §4 safety), and masked by `dry_run` until the operator goes live.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Quiet-hours wraparound (23:00–07:00) or DST handled wrong → kicks in the quiet window | Medium | Medium | Tz-aware compare via `zoneinfo`; explicit wrap test; mirror `reboot/scheduler.py`. |
| Escalation never resets (run window too long) or resets too eagerly | Low | Medium | Recovery window = `max(cooldowns_seconds)`; covered by AC-2 + a reset test. |
| `kick_events` query accidentally counts dry-run rows | Low | Medium | Filter `dry_run = 0` in the query; assert in the cap tests. |
| Caps default-on surprises an operator on upgrade | Low | Low | Documented here + in `config.example.yaml`; `dry_run` gates real action. |

## Implementation Plan

- [ ] **Config** — `BackoffConfig` gains `cooldowns_seconds`, `max_kicks_per_hour`, `max_kicks_per_day` (fail-closed via existing `_require_*` helpers); new `QuietHoursConfig` (start/end HH:MM, IANA `timezone` validated through `zoneinfo`, `override_threshold` whitelisted to tx_rate/retry/signal with `ap_cu_total_min` rejected); `OverrideEntry` gains per-MAC `max_kicks_per_hour`/`max_kicks_per_day`. Update `config.example.yaml`.
- [ ] **Persistence** — add `recent_kick_timestamps(mac, since)` to the `db.Store` Protocol + `Database` (`SELECT ts … WHERE mac=? AND ts>=? AND dry_run=0 ORDER BY ts`); update test doubles in `conftest.py`.
- [ ] **Backoff core** — pure `evaluate_backoff(recent_ts, now, cooldowns, max_hour, max_day)` (in `backoff.py`) returning allow / (reason, retry_after). Keep `BackoffManager` quarantine API intact.
- [ ] **Quiet hours** — `quiet_hours_active(now_wall, cfg)` + per-field stricter merge in `resolution.py`; `Scorer` gains an injected `wall_now_fn` and applies the tightening before `is_bad_state`.
- [ ] **Actor** — add `wall_now_fn = time.time`; consult the per-MAC backoff gate in the fresh-kick path (after the BTM-pending branch, alongside the ADR-0004 gate); log `kick_deferred` with the per-MAC reason.
- [ ] **Tests** — `test_*_acN.py` for AC-1…AC-8 (`time-machine` for the clock), plus the still-missing `tests/test_scorer.py` for the quiet-hours threshold merge. Keep `test_quarantine_ac5` green.

## Related ADRs

- [ADR-0001](./0001-mvp-scope-base-feature.md) — supersedes the §4 backoff + quiet-hours portion of its scope; ADR-0001 stays *Partially Implemented* for its remaining concrete-HA-notifier gap.
- [ADR-0004](./0004-kick-rate-limits.md) — composes with this: ADR-0004 is the global/per-AP limiter, this is the per-MAC budget; both gates apply, and this ADR provides the durable floor ADR-0004 assumes.
- **Follow-up (not yet written):** §3 **AP-saturation gate** (`detection.ap_cu_total_min`) — required before `quiet_hours.override_threshold.ap_cu_total_min` can be honored.

## References

- [`PLAN.md`](../../PLAN.md) §3 (detection), §4 (action policy & backoff), §7 (config schema).
- [Python `zoneinfo`](https://docs.python.org/3/library/zoneinfo.html) — tz-aware quiet-hours evaluation.
