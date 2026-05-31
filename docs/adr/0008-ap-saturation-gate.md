# ADR-0008: AP-Saturation Gate — Only Act on Saturated APs (`detection.ap_cu_total_min`)

**Status:** Accepted
**Date:** 2026-05-31
**Author:** Leonardo Merza

## Context

### Background

`PLAN.md` §3 lists `ap_cu_total_min: 60  # only act on saturated APs` as a detection criterion. The entire purpose of wifi-shepard is to relieve 2.4 GHz airtime contention — a client clinging to a weak or distant AP only *matters* when that AP is actually congested. On an idle AP, a low-rate high-retry client harms no one, and kicking it is gratuitous churn that risks annoying the operator into turning the daemon off.

Yet `scorer.is_bad_state()` never reads `ap_cu_total`, so today the daemon would kick on idle APs — contrary to §3. [ADR-0007](./0007-action-policy-backoff-and-quiet-hours.md) explicitly carved this out (Constraints, "Out of scope") and named it the follow-up that must land before `quiet_hours.override_threshold.ap_cu_total_min` can be honored.

### Current State

- **`scorer.is_bad_state(samples, thresholds, radios)`** — per-sample sliding-window predicate; **all** samples must be bad (it returns `False` on the first good one). Checks `radio ∈ radios`, `signal < signal_dbm_max`, `tx_rate_kbps < tx_rate_kbps_max`, `wifi_tx_attempts > 0`, `retry_pct > retry_pct_max`. **Never reads `ap_cu_total`.**
- **`resolution.resolve_thresholds(mac, config)`** — builds the effective thresholds dict from `_THRESHOLD_FIELDS = (tx_rate_kbps_max, retry_pct_max, signal_dbm_max)`, applying per-MAC `override > global`. `apply_quiet_hours` (ADR-0007) tightens a subset of those per field.
- **`DetectionConfig`** — has `radios`, `tx_rate_kbps_max`, `retry_pct_max`, `signal_dbm_max`; **no `ap_cu_total_min`**. `OverrideEntry` mirrors the same threshold fields.
- **`ClientSnapshot.ap_cu_total: int`** (`controllers/base.py`) — populated by the UniFi backend at `unifi.py:129` as `cu_lookup.get((ap_mac, radio), 0)`: **absent CU → `0`**. Persisted to `client_samples.ap_cu_total` and surfaced by the read-only UI.
- **`config.py`** — `QuietHoursConfig` deliberately omits `ap_cu_total_min` (comment at `config.py:134`), and the loader **rejects** `quiet_hours.override_threshold.ap_cu_total_min` at parse time (fail-closed, `config.py:485`), pointing here.
- **Window** — `scanner.window_samples = 5`, `poll_interval_seconds = 60`: a kick needs **5 bad samples over ~5 minutes**.

### Requirements

1. **Gate kicks on AP saturation** — a MAC is actionable only when its AP's total channel utilization meets `ap_cu_total_min`. An idle AP yields no kicks regardless of how badly a client scores.
2. **Per-MAC overridable** — `ap_cu_total_min` resolves `override > global` like every other threshold (`PLAN.md` §3 shows global `60`, per-MAC override `80`).
3. **Fail closed on unknown CU** — when AP-CU is unavailable, do not act; never guess saturation.
4. **Compose, don't replace** — the existing §3 client criteria and the ADR-0007 / ADR-0004 action gates keep working; this is an *additional necessary condition*, strictly upstream. It can only *withhold* a kick, never cause one.
5. **Default off in code, on in shipped config** — mirror ADR-0007: omitting `ap_cu_total_min` defaults it to `0` (gate disabled = current behavior); `config.example.yaml` / `config.yaml` ship the `PLAN.md` §3 value (`60`). The existing test suite (which omits the key) stays green.

### Constraints

- One process, asyncio, injected clocks; pure decision functions; match `config.py`'s fail-closed validation helpers.
- **Out of scope (deferred — one-line follow-up):** enabling `quiet_hours.override_threshold.ap_cu_total_min`. This ADR is the *prerequisite* gate; honoring the quiet-hours key (drop the parse-time rejection, add the `QuietHoursConfig` field + a stricter-`max()` merge) is a small follow-up and **not release-blocking**. The loader keeps failing closed on that key until then, so an operator who sets it still gets a clear error rather than a silently-ignored guard.

## Options Considered

The structural fork is **how the gate reads `ap_cu_total` across the 5-sample (~5 min) detection window.**

### Option 1: Per-sample predicate extension (Chosen)

**Description:** Add one condition to `is_bad_state` — every window sample must show `ap_cu_total >= ap_cu_total_min` — and make `ap_cu_total_min` another `_THRESHOLD_FIELDS` entry.

**Pros:**
- One-line change in the established predicate; no new module, call, or aggregation.
- Reuses `override > global` resolution (and any future quiet-hours merge) **for free** — `ap_cu_total_min` is just another field in the resolved `thresholds` dict.
- Unknown CU = `0` (UniFi default) `< min` → fails the gate → no kick, **fail-closed automatically**.
- Conservative and consistent: any un-saturated sample → no kick, exactly like the all-samples-bad semantics of every other criterion.

**Cons:**
- A brief CU dip in 1 of 5 samples resets detection — but that is identical to how `signal` / `tx_rate` / `retry` already behave, and erring toward not-acting matches §3's "only act on saturated APs" intent.

### Option 2: Window aggregate (mean/median)

**Description:** Compute the mean (or median) `ap_cu_total` across the window and gate on that.

**Pros:**
- Robust to single-sample CU noise; "saturated over the window" is arguably an average.

**Cons:**
- Introduces a different aggregation semantic than the per-sample criteria (inconsistency).
- mean-vs-median is itself a sub-decision.
- A brief high spike can tip a mostly-idle AP over → **less** conservative.
- More code and a new test shape.

### Option 3: Point-in-time (latest sample)

**Description:** Gate only on the most recent sample's `ap_cu_total` — "is the AP saturated right now, as I act?"

**Pros:**
- Operationally meaningful at the moment of action; cheap.

**Cons:**
- Ignores the sliding window the rest of detection uses.
- One noisy latest sample dominates the decision; can act on a momentary spike.
- Inconsistent with the window-based design.

### Option 4: Separate AP-level gate (actor/pipeline)

**Description:** Keep `is_bad_state` client-only; add a distinct AP-saturation check in the actor/pipeline before kicking.

**Pros:**
- Models CU **honestly** as an *environmental* property — channel utilization is shared by every client on the AP, unlike per-client `signal` / `tx_rate` / `retry`.
- Could read live AP radio stats at act time rather than the per-client snapshot's copy.

**Cons:**
- Splits detection across scorer + actor.
- Forgoes the threshold-resolution + quiet-hours reuse (those operate on the thresholds dict inside the scorer), so per-MAC override and the future quiet-hours tightening must be re-plumbed.
- Extra plumbing for data the per-client snapshot **already carries** (`ap_cu_total`); more moving parts for a home-lab daemon.

## Decision

**Chosen Option:** Option 1 — per-sample predicate extension.

**Rationale:**

1. The data is already in every `ClientSnapshot` (`ap_cu_total`), so the gate is one added condition in the predicate the rest of detection already flows through — no new module, controller call, or aggregation step.
2. Making `ap_cu_total_min` a `_THRESHOLD_FIELDS` entry yields per-MAC override (Requirement 2) and a future quiet-hours tightening for free, uniformly with every other threshold (ADR-0001 AC-6 / ADR-0007).
3. With a 5-sample / ~5-min window, "all samples saturated" is conservative without being brittle, and matches the all-samples-bad contract the predicate already enforces.
4. Option 4's honesty argument (CU is environmental, not per-client) is real and is the strongest case against folding `ap_cu_total` into the thresholds dict. But at home-lab scale the per-client snapshot already carries the AP's CU, so splitting detection across modules buys modeling purity at the cost of reuse and more moving parts. Option 1 wins on pragmatics.

**Forks resolved by this ADR:**

- **Unknown/absent CU is fail-closed by construction.** The UniFi backend writes `0` when no CU is reported (`unifi.py:129`); `0 < ap_cu_total_min` for any positive threshold, so an unknown-CU sample fails the gate → no kick. "Unknown" and "genuinely idle" both correctly map to "don't act," so the conflation is benign for this gate. No separate `None` branch is added, but the predicate reads `(sample.ap_cu_total or 0)` defensively for non-UniFi backends.
- **Comparison is `>=` (floor semantics).** `ap_cu_total_min` is a floor — "act only at or above this utilization." A sample passes the gate iff `ap_cu_total >= ap_cu_total_min`; below it, `is_bad_state` returns `False`.
- **Default `0` = gate disabled.** Omitting `ap_cu_total_min` (or setting `0`) makes the condition vacuously true (`>= 0`), preserving today's behavior and keeping the existing suite green; `config.example.yaml` / `config.yaml` ship `60` and the override example `80`.
- **Quiet-hours key stays rejected.** `quiet_hours.override_threshold.ap_cu_total_min` remains a parse-time error (ADR-0007's loader is untouched here). Honoring it is the deferred one-line follow-up, not part of this ADR.

## Acceptance Criteria

- [ ] **AC-1**: Given `detection.ap_cu_total_min: 60` and a MAC whose window otherwise trips every bad-state criterion, when all samples report `ap_cu_total >= 60`, then `is_bad_state` is `True` (kick path proceeds); when all samples report `ap_cu_total < 60`, then `is_bad_state` is `False`.
- [ ] **AC-2**: Given `ap_cu_total_min: 60` and an otherwise-bad window where one sample reports `ap_cu_total = 30` and the rest `>= 60`, then `is_bad_state` is `False` — the per-sample gate requires *every* sample saturated, mirroring the other criteria.
- [ ] **AC-3**: Given an `overrides:` entry `ap_cu_total_min: 80` for MAC X, then `resolve_thresholds` returns `80` for X and the global `60` for other MACs, so X is actionable only at AP-CU `>= 80`.
- [ ] **AC-4**: Given `ap_cu_total_min` omitted from `detection:` (and no override), then it defaults to `0` and the gate is a no-op — a window that tripped the other criteria before this change still trips it (existing behavior preserved; the suite that omits the key stays green).
- [ ] **AC-5**: Given a sample whose `ap_cu_total` is `0` (the UniFi "no CU reported" default) under `ap_cu_total_min: 60`, then `is_bad_state` is `False` — unknown/idle AP-CU fails closed.
- [ ] **AC-6**: Given `detection.ap_cu_total_min` set to a valid non-negative int in YAML it parses onto `DetectionConfig`; given a negative or non-int value, `load_config` raises a clear `ValueError` at parse time (fail-closed, via the existing `_require_non_negative_int` helper).

## Consequences

### Positive

- Detection finally matches `PLAN.md` §3 — no more kicks on idle APs; the `would_kick` lines logged during the dry-run observation week become representative of real airtime contention instead of noise.
- **Strictly upstream and subtractive:** the gate can only convert a would-kick into a no-kick, so it cannot regress the ADR-0007 caps or the ADR-0004 limiter, and shipping it during the dry-run week can only *reduce* `would_kick` volume — never add risk. A clean confidence point for the eventual flip to `dry_run: false`.
- Reuses the threshold-resolution machinery: per-MAC override is automatic, and the deferred quiet-hours tightening drops in later with no predicate change.
- No schema migration — `ap_cu_total` is already captured per sample.

### Negative

- A bursty AP that dips below the threshold in any single sample of the window defers the kick (same conservative behavior as every other criterion; acceptable given §3's intent).
- `ap_cu_total == 0` conflates "no CU reported" with "genuinely idle." Both correctly map to no-kick here, but a future backend that silently reports `0` for a busy AP would under-act (fail-closed — the safe direction). Documented; the UniFi `cu_lookup` is the source of truth.
- Per-MAC `ap_cu_total_min` is opt-in; a minimal hand-rolled config defaults it to `0` (gate off) until set — mitigated by shipping `60` in the example configs (mirrors ADR-0007's caps) and by `dry_run` defaulting true.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Default `0` leaves the gate silently off in a minimal config → kicks on idle APs | Low | Medium | Ship `ap_cu_total_min: 60` in `config.example.yaml` / `config.yaml`; `dry_run` gates real action; runbook says copy the example. |
| `ap_cu_total = 0` smuggles "unknown" in as "idle"; a future backend reporting `0` for busy APs → under-action | Low | Low | Fail-closed is the safe direction; documented; per-backend CU mapping verified when a 2nd backend lands. |
| `>=` vs `>` off-by-one at the threshold boundary | Low | Low | `>=` (floor) fixed here + AC-1 boundary test. |

## Implementation Plan

- [ ] **Config** — `DetectionConfig` gains `ap_cu_total_min: int = 0`; `OverrideEntry` gains an optional `ap_cu_total_min`; the loader parses both via the existing `_require_non_negative_int` helper; `config.example.yaml` / `config.yaml` set `detection.ap_cu_total_min: 60` (and the override example to `80` per `PLAN.md` §3).
- [ ] **Resolution** — add `"ap_cu_total_min"` to `resolution._THRESHOLD_FIELDS` so `override > global` resolution covers it automatically.
- [ ] **Scorer** — one condition in `is_bad_state`: `if (sample.ap_cu_total or 0) < thresholds["ap_cu_total_min"]: return False`.
- [ ] **Tests** — `tests/test_ap_saturation_gate_acN.py` for AC-1…AC-6 (saturated/idle both directions, mixed window, per-MAC override, default-off back-compat, zero = fail-closed, config parse/reject). Keep `tests/test_scorer.py` and the existing suite green.
- [ ] **Docs** — append the index row; leave the `quiet_hours.override_threshold.ap_cu_total_min` rejection in place with a one-line "deferred follow-up" pointer.

## Related ADRs

- [ADR-0007](./0007-action-policy-backoff-and-quiet-hours.md) — names this as its explicit §3 follow-up; its quiet-hours loader keeps failing closed on `ap_cu_total_min` until a further one-line follow-up enables that key.
- [ADR-0001](./0001-mvp-scope-base-feature.md) — §3 detection scope; this completes the `ap_cu_total_min` criterion the MVP omitted. Per-MAC override mirrors ADR-0001 AC-6 resolution.
- [ADR-0004](./0004-kick-rate-limits.md) / [ADR-0007](./0007-action-policy-backoff-and-quiet-hours.md) — compose downstream; this gate is strictly upstream of both and can only withhold a kick.

## References

- [`PLAN.md`](../../PLAN.md) §3 — detection criteria (`ap_cu_total_min: 60`, override `80`).
- [ADR-0007](./0007-action-policy-backoff-and-quiet-hours.md) Constraints / Related — the deferral that points here.
