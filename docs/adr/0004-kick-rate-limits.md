# ADR-0004: Kick Rate Limits — Global Single-Flight and Per-AP Cap

**Status:** Accepted
**Date:** 2026-05-11
**Author:** Leonardo Merza

## Context

### Background

[ADR-0001 §4](./0001-mvp-scope-base-feature.md) shipped a per-MAC backoff state machine (`backoff.py` — quarantine after N kicks). [ADR-0003](./0003-kick-mechanism-upgrade.md) added a second kick mechanism (BTM) plus a one-cycle deauth fallback, doubling the wire-level call rate per logical kick under `auto`. Neither ADR introduced a **cross-MAC** rate limit, and `PLAN.md` §12 (Safety rails) and §13 (Risks) call out two specific failure modes that aren't covered yet:

1. **Controller rate-limits / locks us out** — the UniFi controller throttles bursts of `cmd/stamgr` and `cmd/devmgr` calls. If many clients go bad-state in the same scan cycle (e.g. a 2.4 GHz radio dies), the actor fires N kicks in quick succession against one HTTP session. UniFi's anti-brute-force layer can revoke the session, leaving the daemon unauthenticated until the next reconnect.
2. **Don't drain an AP all at once** — if 5 IoT clients on the same AP all flag bad-state in one cycle, kicking them all simultaneously cascades: the AP loses 5 associations at once, the clients all re-scan and may re-pick the same broken AP, and the airtime stays bad while the controller logs 5 deauths.

The MVP backoff machinery is **per-MAC only**. It cannot see "we've already kicked 4 things in the last 10 seconds across the whole network" or "this AP just lost 3 clients."

### Current State

- **Actor** (`src/wifi_shepard/actor.py:52`) calls the controller and increments per-MAC backoff. There is no global timer between calls; if `scanner.run_once()` finds 5 bad-state MACs, the actor fires 5 kicks back-to-back inside one event-loop tick.
- **Backoff** (`src/wifi_shepard/backoff.py`) tracks per-MAC `kick_count` only. There is no concept of "kicks per AP" or "kicks per second globally."
- **Config** (`src/wifi_shepard/config.py:78`) has a `BackoffConfig` dataclass with one field (`quarantine_after_kicks`). There is no top-level `safety_rails` or `rate_limits` block.
- **Logging**: a `kick_succeeded` / `kick_no_roam` line fires on the post-kick cycle ([ADR-0003 AC-6](./0003-kick-mechanism-upgrade.md)). There is no `kick_deferred` or `kick_throttled` event yet.
- **`kick_events` table** records only kicks that actually fired. There is no row for "we wanted to kick but rate-limited."
- The `auto` mechanism path means a single logical kick = two wire-level calls (BTM + optional deauth_fallback ~60s later). Any global rate limit must understand that the fallback is part of the same logical kick group.

### Requirements

1. **Global single-flight**: after any kick fires, the actor must wait ≥ `min_seconds_between_kicks` (configurable, default off) before firing any other kick across the entire network. The minimum spacing applies to wire-level calls — both the initial BTM/deauth and any deauth_fallback count.
2. **Per-AP cap**: the actor must enforce ≤ `max_kicks_per_ap_per_window` *logical* kicks against any single AP within `per_ap_window_seconds`. Logical kick = one `attempt_group`. A deauth_fallback on the same `attempt_group` does **not** add to the per-AP counter; it was already counted at the BTM stage.
3. **Deferred kicks are dropped, not queued**: when a candidate is rate-limited, the actor logs a structured `kick_deferred` event and returns. No `record_kick`, no `kick_events` row, no HA notify. The scanner's next poll cycle will re-evaluate the same candidate naturally — if still bad-state, it gets another attempt.
4. **dry_run bypasses rate limits**: `would_kick` lines must continue to fire for every bad-state client even when rate limits are configured. Mirrors the existing dry-run posture (`actor.handle()` returns before backoff/quarantine checks).
5. **Defaults preserve MVP behavior**: with no `safety_rails` block in YAML, the daemon behaves bit-for-bit like ADR-0003. The default value of every new knob is "off" (`0` = unlimited).
6. **Fail-closed validation**: negative integers, non-integer types, or string typos in `safety_rails.*` cause the daemon to log a clear error and exit at config parse time. No half-running.
7. **In-memory state only**: rate-limit state lives on the actor (timestamps, per-AP deques). A daemon restart loses state, allowing a one-shot burst on cold start. The per-MAC backoff cap (ADR-0001) and the absolute per-day kick caps (`PLAN.md` §4) bound the worst case.
8. **Observability**: every deferred kick must produce a structured log line with `mac`, `ap_id`, `reason ∈ {global_rate_limit, per_ap_cap}`, and `retry_after_seconds` (best-effort estimate of when the limit would clear). Operators tune the knobs from these logs.

### Constraints

- One Python process, one event loop ([ADR-0001 §Constraints](./0001-mvp-scope-base-feature.md#constraints)). Rate limits are checked in the actor's `handle()` path — no separate scheduler, no async queue, no `asyncio.sleep` between kicks (would block the scan loop).
- No DB schema changes. ADR-0003 already coordinated a schema bump with the UI sidecar ([ADR-0002 §Risks](./0002-device-history-and-status-ui.md)); adding `kick_deferred` rows would require another sidecar bump, which is out of scope for this ADR.
- No Protocol changes. The `Controller` interface is unchanged.
- SIGHUP config reload must update the thresholds in place; the in-memory rate-limit state (recent-kick timestamps, per-AP deques) is **not** reset on reload — operators are tuning *thresholds*, not requesting a state purge.
- `time.monotonic()` for rate-limit measurements (clock-jump robust); wall-clock is not used.
- The actor is the only place rate limits are consulted. The scorer stays untouched ([CLAUDE.md](../../CLAUDE.md): "don't push vendor specifics into the scorer" — and rate limits are an operator policy, not a detection rule).

## Options Considered

### Option 1: Operator-tuned global single-flight only (no per-AP cap)

**Description:** Add `safety_rails.min_seconds_between_kicks: int` only. Per-AP draining is left for a future ADR.

**Pros:**
- Smallest scope. One config field, one timestamp, one branch in `handle()`.
- Addresses the controller-rate-limit risk (PLAN.md §13 row 3) directly.

**Cons:**
- Leaves the "drain an AP" failure mode unaddressed despite it being explicitly listed in PLAN.md §12. The user's homelab has clusters of IoT on shared APs (WLEDs, plugs, cameras) — exactly the scenario where one bad AP can flag 4+ MACs in one cycle.
- A second small ADR within the same release cycle would create churn on the same config block and the same in-memory state. Bundling them is cheaper than splitting.

### Option 2 (Chosen): Global single-flight + per-AP cap, both opt-in, both in one config block

**Description:** Add a top-level `safety_rails:` block with three fields:
- `min_seconds_between_kicks: int = 0` (0 = off; gates every wire-level kick call)
- `max_kicks_per_ap_per_window: int = 0` (0 = off; counts logical kicks per attempt_group)
- `per_ap_window_seconds: int = 600` (the rolling window the per-AP cap measures over)

The actor holds in-memory state: `_last_kick_at: float | None` (monotonic timestamp) and `_per_ap_kicks: dict[str, deque[float]]` (per-AP timestamp deques, pruned on each check). Both checks fire before any wire-level call; if either trips, the actor logs `kick_deferred` and returns without side-effects.

**Pros:**
- Covers both failure modes PLAN.md flagged with one config surface.
- Defaults are off → zero behavior change for operators who don't opt in. Matches the ADR-0003 conservative-default pattern (`kick_mechanism: deauth`).
- The `kick_deferred` log line makes both limits self-tuning: an operator can grep their logs, see which threshold trips most often, and adjust.
- Per-AP counting at the `attempt_group` level (not per `kick_events` row) is the right semantic — fallback shouldn't double-charge.
- In-memory state is the right scope: cold-start burst is bounded by per-MAC backoff caps and per-day kick caps already in place.

**Cons:**
- Two knobs to tune instead of one. Default-off mitigates: operators don't pay for what they don't use.
- The per-AP deque grows under sustained kick storms (one entry per kick); pruning on each check keeps it bounded by `max_kicks_per_ap_per_window` after the first window elapses.
- A deferred kick that *would* have happened just shifts to the next scan cycle. If both rate limits stay tight and the scorer keeps flagging the same MAC, the user could see repeated `kick_deferred` lines for the same MAC. That's the right behavior — the operator wanted the limit — but the log volume is real.
- Restart loses state. A worst-case crash-loop right after a kick could re-fire kicks faster than `min_seconds_between_kicks`. Bounded by per-MAC backoff (5 kicks → quarantine, ADR-0001 §4); not adding persistence for one cold-start burst.

### Option 3: Hard-coded defaults, no config knob

**Description:** Bake in `min_seconds_between_kicks=5` and `max_kicks_per_ap_per_window=3 / per_ap_window_seconds=300` as constants. No YAML.

**Rejected because:** the user's existing homelab patterns (specific IoT clusters, specific AP capacities) are not generic. Hard-coding a default that's "right" for one network is wrong for another. The fail-closed-validation-but-explicit-YAML pattern matches every other tunable in this codebase ([ADR-0001 §7](./0001-mvp-scope-base-feature.md), [ADR-0003 Decision](./0003-kick-mechanism-upgrade.md#decision)).

### Option 4: Token-bucket rate limiter (refilling at a rate)

**Description:** Use a leaky-bucket / token-bucket algorithm: each kick consumes a token; tokens refill at `R` per second up to a `B`-token burst.

**Rejected because:** the homelab kick rate is naturally low (per-MAC backoff caps total kicks/day at ~5N for N MACs). Token-bucket gives precise burst control we don't need, at the cost of two more knobs operators have to reason about (rate, burst). A simple "min N seconds since last kick" + "max K kicks per rolling window per AP" is easier to explain in YAML and easier to tune from logs.

### Option 5: Persist rate-limit state to SQLite

**Description:** Store recent-kick timestamps in a `recent_kicks` table so restarts don't reset the limiter.

**Rejected because:** the worst-case cold-start burst is bounded by per-MAC backoff (5 kicks → quarantine). Adding a table, migration, and a sidecar bump for "five kicks happened in 10s once after a daemon restart" is not worth it. ADR-0003 already documented the "restart loses in-flight attempt_groups" risk and accepted the same trade-off.

## Decision

**Chosen Option:** Option 2 — Global single-flight + per-AP cap, both opt-in, in one new `safety_rails:` config block.

**Rationale:**

1. The two failure modes PLAN.md flagged (controller lockout, AP drain) are distinct mechanisms that share a code path: both are checks before the actor's wire-level call. Bundling them into one ADR + one config block + one in-memory state container is cheaper than splitting.
2. Default-off matches the ADR-0001 / ADR-0003 conservative-default discipline. An operator who upgrades and does nothing sees zero behavior change. The README will recommend `min_seconds_between_kicks: 5` for UniFi as a safe starting point (informed by `aiounifi`'s existing 1s session-retry posture).
3. `kick_deferred` log lines make both limits self-tuning — the operator sees exactly which threshold trips and at what cadence, and adjusts. No metrics endpoint required for the first tuning pass.
4. Per-AP counting at the `attempt_group` level keeps ADR-0003's BTM-then-deauth-fallback semantics intact. A logical kick is still one logical kick regardless of how many wire calls it takes.
5. In-memory state is the right scope. Cold-start burst is double-bounded (per-MAC backoff cap + per-day kick cap). Not persisting is consistent with ADR-0003's stance on `_pending_btm`.
6. No DB schema changes → no UI sidecar bump → no coordinated release. Smaller blast radius than ADR-0003.
7. The actor is the only place that touches rate limits. The scorer, scanner, backoff, and controller backends are untouched. Future backends (Omada, OpenWRT) get rate limits for free.

**Implementation forks resolved by this ADR:**

- **State location (Fork A):** In-memory on the `Actor` instance. Lost on restart; bounded by per-MAC backoff and per-day caps. Not persisted.
- **Granularity (Fork B):** Two knobs — global (per-process) and per-AP. No per-radio, per-VLAN, or per-client-group rate limits in this ADR. Those would be additive future ADRs.
- **Algorithm (Fork C):** Naive "min interval" for global; rolling deque-pruned window for per-AP. No token bucket.
- **Deferred-kick handling (Fork D):** Drop and log. No queue, no `asyncio.sleep`. The scan loop re-evaluates on the next cycle naturally.
- **Backoff interaction (Fork E):** A deferred kick does **not** call `backoff.record_kick(mac)`. The kick didn't happen, so the per-MAC budget is not consumed. (Symmetrically, ADR-0003's deauth_fallback didn't double-count; this ADR preserves that.)
- **Dry-run interaction (Fork F):** Rate limits are skipped in dry_run, matching existing actor.handle() behavior where dry_run returns before backoff/quarantine checks. Dry-run's job is to show every candidate.
- **Fallback interaction (Fork G):** Global rate limit gates the deauth_fallback wire call (it's a wire call). Per-AP cap does **not** count the fallback (it's the same logical kick that was already counted at the BTM stage). If the fallback is globally-rate-limited, the actor leaves `_pending_btm[mac]` in place and the next cycle retries.
- **SIGHUP reload (Fork H):** Threshold values update in place; in-memory state (`_last_kick_at`, `_per_ap_kicks`) is not reset. Operators are tuning, not purging.
- **Observability (Fork I):** Structured `kick_deferred` log lines only. No new DB table, no UI surface, no Prometheus counters in this ADR. Those are explicit future-ADR candidates.
- **Clock source (Fork J):** Both `Actor` and `KickRateLimiter` take an injected `now_fn: Callable[[], float] = time.monotonic` constructor kwarg. Production wiring uses the default. Tests construct with a fake clock (a list-backed callable or a `[t]` mutable nonlocal) so AC-3 and AC-8 can simulate time without `monkeypatch`-ing the `time` module globally.
- **Rate-limiter record API (Fork K):** Two methods, not one flag. `record_kick(ap_id, now)` updates both the global timestamp and the per-AP deque (fresh kicks). `record_wire_call(now)` updates only the global timestamp (deauth_fallback wire call). The actor calls the right one at each site; the method names document the intent at the call site.

## Acceptance Criteria

- [ ] **AC-1**: Given a config with no `safety_rails:` block, when the actor handles N bad-state clients in one cycle, then (a) `force_reconnect_calls` / `btm_calls` sequences match the baseline produced without the rate-limit feature, (b) `kick_events` row count equals N, (c) `backoff.kick_count` is incremented exactly once per MAC, and (d) **no** `kick_deferred` log line is emitted. The baseline is the existing `tests/test_actor_btm.py` shape for `kick_mechanism=deauth`.
- [ ] **AC-2**: Given `safety_rails.min_seconds_between_kicks: 30` and two bad-state MACs on different APs in one cycle, when the actor handles them, then the first MAC's kick fires normally, the second MAC produces a `kick_deferred` log line with `reason='global_rate_limit'` and a `retry_after_seconds` field, **no** wire-level kick is sent for the second MAC, **no** `kick_events` row is written for the second MAC, and `backoff.kick_count` for the second MAC remains at 0.
- [ ] **AC-3**: Given AC-2's state and the simulated clock advanced past `min_seconds_between_kicks`, when the scanner's next cycle handles the still-bad-state second MAC, then the kick fires normally and a `kick_events` row is written.
- [ ] **AC-4**: Given `safety_rails.max_kicks_per_ap_per_window: 2` and `per_ap_window_seconds: 600`, and three bad-state MACs all on `ap_id=ap1` in one cycle, when the actor handles them (with `min_seconds_between_kicks: 0` so global gating doesn't interfere), then 2 kicks fire against `ap1`, the third MAC produces a `kick_deferred` log line with `reason='per_ap_cap'` and `ap_id='ap1'`, and the per-MAC backoff for the third MAC remains at 0.
- [ ] **AC-5**: Given an in-flight `_pending_btm` `attempt_group=G` for MAC `M` on `ap_id=ap1`, and `safety_rails.max_kicks_per_ap_per_window: 1` (already consumed by the BTM stage), when the next scan cycle scores `M` still bad-state on `ap1` and the actor handles it, then the deauth_fallback fires normally under the same `attempt_group=G` — the per-AP cap does **not** block it (it's the same logical kick).
- [ ] **AC-6**: Given `scanner.dry_run: true` and `safety_rails.min_seconds_between_kicks: 999`, when the actor handles two bad-state clients in one cycle, then **both** clients log `would_kick`, **no** `kick_deferred` lines fire, and the rate-limit state is not updated (dry-run is observation, not action).
- [ ] **AC-7**: Given negative or non-integer values in any `safety_rails.*` field (e.g. `min_seconds_between_kicks: -1`, `max_kicks_per_ap_per_window: "many"`), when the daemon loads the config at startup, then it raises a `ValueError` mentioning the field name and exits — matching ADR-0001 §Decision fail-closed posture.
- [ ] **AC-8**: Given a config reload via SIGHUP that changes `safety_rails.min_seconds_between_kicks` from `0` to `60`, when the next bad-state client is handled within 60s of any previously-fired kick, then the new threshold gates the kick (the in-memory `_last_kick_at` is honored across the reload; thresholds update in place).

## Consequences

### Positive

- Two new safety rails operators can opt into: prevents controller lockout (UniFi anti-brute-force) and prevents single-AP drain cascades.
- Defaults are off → zero behavior change unless the operator opts in. Matches the ADR-0001/-0003 conservative-default discipline.
- `kick_deferred` structured logs make the rate limits self-tuning. No metrics endpoint needed.
- Per-AP counting at the `attempt_group` granularity keeps ADR-0003's "one logical kick = one logical count" invariant intact.
- No DB schema changes, no UI sidecar bump, no Protocol changes — small blast radius. Future backends (Omada, OpenWRT) get rate limits for free because the limits live in the actor, not the controller.
- The `dry_run` posture (limits bypassed) makes the observe-only period more useful: operators see every candidate, including ones that *would* have been deferred under rate limits, so they can size the thresholds before flipping `dry_run: false`.

### Negative

- Two new knobs to tune instead of one or zero. Default-off mitigates; README documents safe starting values.
- Restart loses rate-limit state (small cold-start burst possible). Bounded by per-MAC backoff cap + per-day kick cap. Not adding persistence.
- A deferred kick produces a `kick_deferred` log line every scan cycle the MAC stays bad-state and gated — log volume can grow under sustained rate-limit pressure. Operators tune thresholds to reduce this.
- The per-AP deque grows up to `max_kicks_per_ap_per_window` entries per AP within `per_ap_window_seconds`; bounded but allocates per kick. Memory cost is negligible for homelab AP counts.
- No `kick_deferred` row in `kick_events` — deferred kicks are log-only. A future ADR (or this one's extension) could persist them, but that requires another schema bump.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Operator sets `min_seconds_between_kicks` too high; legitimate kicks chronically deferred | Medium | Medium | `kick_deferred` log lines surface this immediately. README recommends ≥1-week dry-run period with rate limits enabled to size thresholds. Default-off means operator has to opt in deliberately. |
| Per-AP cap tripping during a real AP failure (radio dies, all clients flagged) prevents the daemon from doing its job | Medium | Medium | Per-AP cap is *per AP*, not global — a failed AP getting capped just means kicks stop on that AP. Other APs continue to be serviced. Operator escape: lower `per_ap_window_seconds` or raise `max_kicks_per_ap_per_window`. The mass-disassoc detector (future ADR) will eventually pause kicks during these events. |
| Cold-start burst exceeds `min_seconds_between_kicks` once on every restart | Low | Low | Per-MAC backoff cap (5 → quarantine, ADR-0001) and per-day kick caps bound the absolute worst case. Not persisting rate-limit state matches ADR-0003's `_pending_btm` stance. |
| Deferred kick of a still-bad-state MAC re-evaluates on every scan cycle, creating log noise | Medium | Low | Structured `kick_deferred` rows can be filtered / rate-limited by the operator's log aggregation. The alternative (suppressing repeat deferrals) hides a real signal: a chronically-deferred MAC means the rate-limit is too tight or quarantine should fire. Leaving every cycle visible is the right default. |
| BTM fallback gets globally-rate-limited indefinitely, leaving `_pending_btm` stuck | Low | Medium | Per-MAC backoff still increments on the BTM stage. Once `quarantine_after_kicks` is reached, quarantine returns early and the daemon stops attempting that MAC. The `_pending_btm` entry stays but is harmless — the fallback path's `ap_id` check guards re-firing. |
| Negative-int validation gap in `safety_rails` accepts `0` as "off" but also accepts `0.5` from a YAML float | Low | Low | Config parser coerces via `int(...)` and validates `>= 0` in `build_config`. AC-7 directly tests this with negative and non-int inputs. |

## Implementation Plan

Build order matches ADR-0003's shape: schema/state first, config plumbing second, actor logic third, then logging, then docs. Each phase ends with something testable.

- [ ] **Phase 0 — rate-limiter module** (`src/wifi_shepard/rate_limit.py`): new module with a `KickRateLimiter` class. Constructor: `KickRateLimiter(*, min_seconds_between_kicks: int, max_kicks_per_ap_per_window: int, per_ap_window_seconds: int)`. Methods (per Fork K):
  - `can_kick(ap_id: str, now: float) -> tuple[bool, str | None, float | None]` → `(allowed, reason, retry_after_seconds)`; checks global single-flight, then per-AP cap.
  - `record_kick(ap_id: str, now: float) -> None` → fresh kick; updates both the global timestamp and the per-AP deque.
  - `record_wire_call(now: float) -> None` → fallback wire call; updates only the global timestamp.
  Holds `_last_kick_at: float | None` and `_per_ap_kicks: dict[str, deque[float]]`. Pure unit-testable. Tests: `tests/test_rate_limiter.py` (no AC; structural).
- [ ] **Phase 1 — config plumbing** (`src/wifi_shepard/config.py`): new `SafetyRailsConfig` dataclass (3 fields), added to `Config`; parser validates `>= 0` and integer typing, fails closed otherwise. `build_config` accepts `safety_rails` kwargs. `load_config_from_path` reads `safety_rails:` block. Tests: `tests/test_config_safety_rails_ac7.py` (AC-7).
- [ ] **Phase 2 — actor wiring** (`src/wifi_shepard/actor.py`): instantiate `KickRateLimiter` from config; both `Actor` and `KickRateLimiter` take an injected `now_fn: Callable[[], float] = time.monotonic` so tests can simulate clock advancement without `monkeypatch`. Check `can_kick(client.ap_id, self.now_fn())` before each wire-level kick (BTM, deauth, deauth_fallback). On block, log `kick_deferred`, return — no `record_kick`, no `db.insert_kick`, no HA notify, no rate-limiter state update. On a fresh-kick success call `rate_limiter.record_kick(client.ap_id, now)`; on a deauth_fallback success call `rate_limiter.record_wire_call(now)` (per Fork K). Tests: `tests/test_rate_limit_global_ac2_ac3.py` (AC-2 + AC-3), `tests/test_rate_limit_per_ap_ac4.py` (AC-4), `tests/test_rate_limit_fallback_ac5.py` (AC-5).
- [ ] **Phase 3 — defaults + dry-run + SIGHUP** (`src/wifi_shepard/actor.py` + `scanner.py`): verify `dry_run` bypass (AC-6), verify default-off preserves ADR-0003 behavior (AC-1), verify SIGHUP threshold update in place (AC-8). Mostly tests, minimal new code. Tests: `tests/test_rate_limit_default_off_ac1.py` (AC-1), `tests/test_rate_limit_dry_run_ac6.py` (AC-6), `tests/test_rate_limit_sighup_ac8.py` (AC-8).
- [ ] **Phase 4 — docs**: update `config.example.yaml` with a commented-out `safety_rails:` block and recommended starting values (`min_seconds_between_kicks: 5`, `max_kicks_per_ap_per_window: 3`, `per_ap_window_seconds: 600`). Note in README / CLAUDE.md that this is an opt-in feature with a ≥1-week dry-run tuning period recommended.

## Related ADRs

- [ADR-0001](./0001-mvp-scope-base-feature.md) — defines the per-MAC backoff state machine this ADR composes with (this ADR's deferred kicks do **not** increment `backoff.record_kick`, matching the "one logical kick = one budget unit" invariant) and the conservative-default discipline this ADR mirrors.
- [ADR-0003](./0003-kick-mechanism-upgrade.md) — defines the `attempt_group` UUID and the BTM-then-deauth_fallback path this ADR's per-AP cap counts at logical-kick granularity (not per `kick_events` row).

Anticipated follow-ups (not yet written):

- **ADR for mass-disassoc detector** — when N clients across the network all flag bad-state simultaneously (firmware push, channel re-plan), pause **all** kicks for ≥5 minutes. Composes with this ADR's global single-flight but operates on detection signal, not a hard count.
- **ADR for `kick_deferred` persistence** — promote the log line into a `kick_events`-like table once operators want SQL queries against deferral history. Requires a UI sidecar bump.
- **ADR for Prometheus `/metrics` exporter** — surface `kick_deferred_total{reason="global_rate_limit"}`, `kick_deferred_total{reason="per_ap_cap"}`, `kicks_per_ap_window{ap_id="..."}` for Grafana dashboards. Already anticipated by ADR-0001/-0002/-0003.
- **ADR for network-wide pause switch (HA)** — `switch.wifi_shepard_paused` flipping a runtime kill switch via MQTT or HA REST. Operator UX, not a rate limit.

## References

- [`PLAN.md`](../../PLAN.md) §12 Safety rails (Single-flight kicks, Per-AP kick cap), §13 Risks (Controller rate-limits / locks us out).
- [`CLAUDE.md`](../../CLAUDE.md) — "don't push vendor specifics into the scorer" rule (rate limits live in the actor, not the scorer or controller).
- [Python `time.monotonic()`](https://docs.python.org/3/library/time.html#time.monotonic) — clock-jump-robust monotonic clock used by the rate limiter.
- [Python `collections.deque`](https://docs.python.org/3/library/collections.html#collections.deque) — O(1) append + pop-left used by the per-AP windowed counter.
- [`aiounifi`](https://github.com/Kane610/aiounifi) — the library wrapping the UniFi controller's HTTP session. The README's recommended starting value (`min_seconds_between_kicks: 5`) is a homelab-conservative default; operators tune from `kick_deferred` log frequency, not from a library-specific number.
