# ADR-0009: Disable-able Detection Criteria — `null` Turns a Client Signal Off

**Status:** Accepted
**Date:** 2026-06-14
**Author:** Leonardo Merza

## Context

### Background

`scorer.is_bad_state()` flags a client only when **every** sample in the window satisfies **all** client criteria at once: weak `signal` **and** low `tx_rate` **and** high `retry_pct` (plus the radio filter and the ADR-0008 AP-saturation gate). The three client criteria are a hard `AND`.

Live data from the first dry-run day exposed the limitation. The only device that ever tripped the full `AND` (an Espressif chip) self-corrected within minutes, while a genuinely mis-placed device — `shelly1 laundry room`, weak signal (`<-70` in 23/26 saturated-2.4GHz samples) and retry-heavy — never qualified because its `tx_rate` stayed above 12 Mbps. For low-traffic IoT, `tx_rate` and `retry_pct` are noisy: a device that barely transmits looks "slow", and a tiny transmit count makes `retry_pct` swing wildly. **Signal (distance from the AP) is the meaningful "should re-roam" indicator**, and the operator had no way to act on signal + saturation alone short of hacking the other two thresholds to neutral values.

### Current State

- **`scorer.is_bad_state`** reads `thresholds["signal_dbm_max"]`, `["tx_rate_kbps_max"]`, `["retry_pct_max"]` as **required** keys — there is no "skip this criterion" path.
- **`DetectionConfig`** — `tx_rate_kbps_max: int`, `retry_pct_max: int`, `signal_dbm_max: int` (non-optional, with defaults `12000 / 30 / -70`). The loader coerces each with `int(detection_data.get(key, default))`, so a YAML `null` would crash (`int(None)`).
- **`resolution.resolve_thresholds`** already carries `None` through (per-MAC override `None` = "inherit"); **`apply_quiet_hours`** does `min()/max()` against the resolved value and would `TypeError` on `None`.
- **AP-saturation gate (ADR-0008)** is already `.get(..., 0)`-optional and disabled by default — the precedent for an opt-out criterion.

### Requirements

1. **Disable any client criterion via config** — setting `detection.<criterion>: null` turns that signal off; a "signal + saturation only" mode (and any other subset) becomes expressible without threshold hacks.
2. **Omission keeps the active default** — absent key → shipped default (criterion on). The existing suite, which omits nothing-or-some keys, stays green.
3. **Fail safe on an all-off config** — never act on radio + saturation alone (that would flag every saturated client). Reject an all-null trio at load time, and have the scorer refuse it as defense-in-depth.
4. **Compose with the rest** — the radio filter, AP-saturation gate, sliding window, and ADR-0007/0004 action gates are untouched; a disabled criterion is simply skipped, the remaining active ones still all-`AND`.
5. **Quiet hours must not break or surprise** — tightening a disabled criterion is a no-op (it stays disabled), never a re-enable.

### Constraints

One process, asyncio, pure decision functions; match `config.py`'s fail-closed validation helpers. Per-MAC *disabling* is out of scope — `overrides` use `None` to mean "inherit", so an override can change a criterion's value but not turn it off; disabling is global-only for now.

## Options Considered

### Option 1: `None`-means-off in the resolved thresholds (Chosen)

Make the three `DetectionConfig` criteria `int | None`; parse explicit YAML `null` to `None` (absent → default); `is_bad_state` skips any criterion whose threshold is `None`.

**Pros:** one predicate already flows the `thresholds` dict, so "skip if None" is a local change; reuses the existing resolution machinery for free; mirrors ADR-0008's opt-out precedent; "signal + saturation only" is one `null`-pair in config.
**Cons:** an all-null trio is now expressible and must be guarded (config rejects it; scorer fails safe).

### Option 2: A `criteria: [signal, tx_rate, retry]` enable-list

A separate list naming which criteria are active.

**Pros:** explicit; can't accidentally null a value you meant to keep.
**Cons:** a second source of truth beside the threshold values; the resolution + quiet-hours layers would have to learn the list; more surface for a home-lab daemon. Redundant with "a value or null".

### Option 3: Separate AP/environment gate, leave criteria as-is (status quo + tuning)

Don't add disabling; tell operators to widen `tx_rate_kbps_max` to catch weak-but-fast devices.

**Pros:** no schema change.
**Cons:** doesn't model intent — you can't say "ignore throughput"; widening `tx_rate` to ∞ to fake it is exactly the hack this ADR removes; retry stays a forced criterion.

## Decision

**Chosen Option:** Option 1 — `None`-means-off, validated against an all-off trio.

**Rationale:** the thresholds dict is already the single channel every layer (scorer, resolution, quiet hours) reads, so representing "off" as `None` in that dict is the smallest change that composes everywhere. It matches the ADR-0008 `ap_cu_total_min` opt-out precedent and keeps one source of truth (a value, or `null`).

**Forks resolved:**

- **Absent vs. explicit-null.** Absent key → active default (back-compat, Req 2). Explicit `null` → disabled. The loader's `_optional_int_field` distinguishes the two; bool is rejected (YAML `yes/no` is an int subclass), as elsewhere.
- **All-off is rejected at load, refused at scoring.** `build_config`/`load_config` raise a clear `ValueError` when all three are null (Req 3). `is_bad_state` independently returns `False` if handed an all-None thresholds dict, so a direct caller (test/embed) can't trip "every saturated client is bad".
- **Quiet hours skips disabled criteria.** `apply_quiet_hours` only tightens a criterion that is currently non-`None`; a disabled one stays disabled (no re-enable, no `TypeError`). Re-enabling during quiet hours is a possible future enhancement, intentionally not built.
- **Per-MAC disabling deferred.** Overrides keep `None` = "inherit"; disabling is global. Documented; revisit if a per-device opt-out is ever needed.

## Acceptance Criteria

- [x] **AC-1**: With `tx_rate_kbps_max` and `retry_pct_max` set to `null` (signal + saturation only), a weak-signal client on a saturated AP is flagged **regardless** of its (good) tx_rate / (low) retries; with all three active the same client is spared; a strong-signal client on the same AP is spared either way.
- [x] **AC-2**: `is_bad_state` returns `False` when handed a thresholds dict with all three client criteria `None`, even for a fully-saturated window (fail safe — never act on radio + saturation alone).
- [x] **AC-3**: Omitting a criterion key in `detection:` keeps its active default (`tx_rate_kbps_max` → `12000`), preserving current behavior.
- [x] **AC-4**: Explicit YAML `null` parses to `None` on `DetectionConfig`; an all-null trio raises a clear `ValueError` at load time (and via `build_config`), naming "at least one client criterion".
- [x] **AC-5**: `apply_quiet_hours` leaves a disabled (`None`) criterion `None` (no re-enable, no `TypeError`) while still tightening the active ones.
- [x] **AC-6**: With one criterion disabled and the others active, the remaining criteria still all-`AND`: a client violating only one active criterion is spared; one violating all active criteria is flagged.

## Consequences

### Positive

- Operators can express "act on weak signal on a congested AP, ignore throughput/retries" — the mode the live data showed is needed (`shelly1`-class devices) — with two `null`s, no threshold hacks.
- Strictly opt-in: omitting keys = today's behavior; the existing suite stays green and the shipped config is unchanged.
- Reuses resolution + quiet-hours machinery; no new module or aggregation.

### Negative

- An all-null trio is newly expressible; mitigated by load-time rejection + scorer fail-safe.
- Per-MAC disabling isn't supported (override `None` = inherit); global-only for now.
- "Signal + saturation only" is more aggressive (fewer conditions) — a low-traffic device that drifts weak on a busy AP can now be re-roamed. Mitigated by `dry_run`, the ADR-0007 caps/cooldowns/quarantine, and the AP-saturation gate.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Operator nulls all three → daemon silently never (or, without the guard, always) acts | Low | Medium | Load-time `ValueError` + scorer fail-safe (`False`) |
| Over-aggressive signal-only mode re-roams healthy low-traffic IoT | Medium | Low | AP-saturation gate still required; ADR-0007 caps/quarantine; `dry_run` first; allowlist |
| `null` reaches `min()/max()` in quiet hours | Low | Low | `apply_quiet_hours` guards on `out.get(key) is not None` + AC-5 |

## Implementation Plan

- [x] **Config** — `DetectionConfig` criteria → `int | None`; `build_config` params → `int | None` + all-null `ValueError`; `_optional_int_field` parses absent→default / null→None / int (reject bool).
- [x] **Scorer** — `is_bad_state` reads the three via `.get()`, skips any `None`, returns `False` if all three are `None`.
- [x] **Resolution** — `apply_quiet_hours` guards each tightening on a non-`None` current value (`resolve_thresholds` already carries `None`).
- [x] **Tests** — `tests/test_detection_disable_criteria_ac9.py` for AC-1…AC-6; existing detection/quiet-hours/config suites stay green.
- [x] **Docs** — index row; `config.example.yaml` notes `null` disables a criterion.

## Related ADRs

- [ADR-0008](./0008-ap-saturation-gate.md) — the opt-out-criterion precedent (`ap_cu_total_min`, `.get(..., 0)`); this generalizes opt-out to the three client signals.
- [ADR-0007](./0007-action-policy-backoff-and-quiet-hours.md) — quiet-hours tightening, now `None`-aware; caps/quarantine bound the more-aggressive modes this enables.
- [ADR-0001](./0001-mvp-scope-base-feature.md) — §3 detection scope and `override > global` resolution this builds on.

## References

- [`PLAN.md`](../../PLAN.md) §3 — detection criteria.
- `src/wifi_shepard/scorer.py`, `src/wifi_shepard/config.py`, `src/wifi_shepard/resolution.py`.
