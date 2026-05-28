# ADR-0006: Reboot Remediation — Proactive Scheduling + Reactive Escalation

**Status:** Proposed
**Date:** 2026-05-24
**Author:** Leonardo Merza

## Context

### Background

[ADR-0005](./0005-device-identification-and-reboot-backend.md) decided **how** the daemon reboots a device: identify it via the Home Assistant device registry and trigger its reboot path (restart `button`, else power `switch`), with an explicit per-MAC override fallback, all behind an opt-in `reboot.eligible:` list. ADR-0005 deliberately stopped there — it resolves *who the device is and how it would be rebooted*, not *when*.

This ADR decides **when** a reboot fires. It is the other half of [Issue #9](https://github.com/ljmerza/wifi-shepard/issues/9): the rotted-radio remediation that re-roaming cannot cure. The motivating incident — kitchen ESP32 "Fridge" at a *healthy* −58 dBm yet 62% packet loss, fixed only by a power-cycle — has two viable remediations, and the issue recommends **both**:

- **Proactive:** scheduled reboots of known-flaky IoT (e.g. nightly/weekly). No detection. Prevents rot from ever reaching the 62%-loss state. Cheap and effective at homelab scale.
- **Reactive:** detect that a device is degraded *despite adequate signal and despite re-roaming*, then escalate to a reboot. Fixes the incident precisely, but needs detection machinery the daemon does not have today.

This ADR commits to both, **phased**: the proactive scheduler ships first (it builds almost entirely on ADR-0005's already-resolved reboot targets and adds no detection or new network capability), and the reactive escalation follows. Bundling them in one ADR mirrors [ADR-0004](./0004-kick-rate-limits.md), which combined two related safety rails rather than splitting, because they share one config surface and one in-memory state container — here, proactive and reactive reboot share the `reboot:` block, the `Rebooter` surface, the `reboot_events` audit table, the cooldown/daily-cap machinery, and the dry-run posture.

### Current State

- **ADR-0005 gives the resolved target, not the action.** ADR-0005 (Proposed) defines `reboot.enabled`, `reboot.resolver: home_assistant`, `reboot.eligible:`, and `reboot.overrides:`, plus the resolution ladder that turns an eligible MAC into a concrete HA reboot entity. There is **no `Rebooter` implementation** yet, and ADR-0005 depends on a concrete HA client landing first (today `notify/__init__.py` is only a `Notifier` Protocol stub; `home_assistant:` is injected, not parsed from YAML — `config.example.yaml:8-9`).
- **The scorer's bad-state predicate is tuned for clingers and misses good-RSSI rot.** `scorer.is_bad_state()` is an **AND** of (signal < `signal_dbm_max`) ∧ (tx_rate < `tx_rate_kbps_max`) ∧ (retry_pct > `retry_pct_max`), over a sliding window (`controllers/base.py` `ClientSnapshot`; `scorer.py`). Because the Fridge's signal (−58) was *above* the default −70 threshold, this predicate **never fires** for it. So the reactive trigger cannot be a tweak to the existing predicate — it needs a signal the predicate doesn't have.
- **`ClientSnapshot` lacks the signals the reactive trigger needs.** It carries `mac`, `signal`, `tx_rate_kbps`, `tx_retries`, `wifi_tx_attempts`, `radio`, `ap_id`, `ap_cu_total` — **no packet-loss %, no device IP, no UniFi `satisfaction`**. Passive `tx_retries / wifi_tx_attempts` is the only in-band degradation proxy today, and it is noisy (airtime contention inflates retries for every client on a busy channel, not just sick radios).
- **The escalation seam already exists.** [ADR-0003](./0003-kick-mechanism-upgrade.md) emits `kick_no_roam` when a kick fails to move a client, and [ADR-0001](./0001-mvp-scope-base-feature.md) §4 quarantines a MAC after N kicks (`backoff.py`). "This MAC keeps misbehaving despite kicks → it doesn't need another roam, it needs a reboot" is the natural reactive trigger gate.
- **Reusable safety scaffolding.** `scanner.dry_run` (`would_kick`), the `KickRateLimiter` cooldown/window pattern (ADR-0004, `rate_limit.py`), per-MAC `backoff`, the `kick_events` audit table + `db.insert_kick`, and the `Notifier` are all directly analogous to what reboot needs (`would_reboot`, a reboot cooldown/daily cap, a `reboot_events` table + `insert_reboot`, reboot notifications).

### Requirements

1. **Proactive scheduler.** Reboot opt-in MACs on a configured cadence (e.g. a daily time, or an interval), reusing ADR-0005's resolved target. No detection required.
2. **Reactive escalation.** Reboot an eligible device only when it is degraded *and* the degradation is not explained by signal *and* re-roaming has already failed (quarantine / repeated `kick_no_roam`). Never as a first action.
3. **Reactive trigger uses a signal the AND-predicate lacks.** Because the existing predicate misses good-RSSI rot, the trigger is built on an **active reachability probe** (loss%/latency over a window), corroborated by passive retry-% / `satisfaction`. The trigger must not fire on a single bad sample.
4. **Escalation-only, never first-action.** A transient blip must never cause a power-cycle. Reboot fires only after the re-roam ladder (kick → reconnect) has demonstrably failed for that MAC.
5. **Dry-run first.** With `reboot.dry_run: true`, emit a structured `would_reboot` line and perform **no** network call — mirroring `would_kick`.
6. **Hard cooldown + daily cap per device.** Never reboot-loop a device. Reuse the ADR-0004 cooldown/window pattern.
7. **Allowlist + opt-in absolute.** Allowlisted MACs are never rebooted; only `reboot.eligible:` MACs are (ADR-0005 Requirements 3–4 carry forward).
8. **Audit + observability.** Every reboot (and every `would_reboot`) writes a `reboot_events` row and a structured log line, surfaced in the read-only UI history (ADR-0002).
9. **SIGHUP reload.** Schedule, thresholds, and caps re-read on SIGHUP; in-flight cooldown state is not purged (ADR-0004 reload posture).
10. **Defaults preserve current behavior.** No `reboot:` block (or `reboot.enabled: false`) → no reboots, no scheduler, no probe loop. Bit-for-bit unchanged.

### Constraints

- **Depends on ADR-0005** (resolved reboot target + concrete HA client). This ADR cannot ship before ADR-0005's resolver exists.
- One Python process, one event loop ([ADR-0001 §Constraints](./0001-mvp-scope-base-feature.md)). The proactive scheduler and the active probe are `asyncio` tasks, not separate processes; neither may block the scan loop.
- **`Rebooter` is a new surface, not a `Controller` method.** UniFi cannot reboot a client. The `Rebooter` Protocol lives in a new `remediators/` package (resolving the "controllers/ vs remediators/" fork the issue raised), sibling to `controllers/`. The scorer/scanner never import it.
- **Don't push device specifics into the scorer** ([CLAUDE.md](../../CLAUDE.md)). The reactive degradation predicate is a new, brand-agnostic scoring function fed by the probe loop; it does not reach into HA or any vendor API.
- **DB schema bump coordination.** Adding `reboot_events` is a schema change. [ADR-0004 §Constraints](./0004-kick-rate-limits.md) and [ADR-0002 §Risks](./0002-device-history-and-status-ui.md) note schema bumps must be coordinated with the UI sidecar. This ADR owns that bump for `reboot_events`.
- **The active probe needs the device IP, which `ClientSnapshot` does not carry** — an open resolution point (see Phase 0 / Risks), not a settled fact.

## Options Considered

Two independent forks: **(A) which remediation model**, and **(B) the reactive trigger signal**.

### Fork A — remediation model

#### A1: Proactive only

**Description:** Scheduled reboots of opt-in MACs; no detection.

**Pros:** Simplest possible build — a scheduler on top of ADR-0005's resolver, zero new detection or probe capability. Prevents rot before it manifests. Would have prevented the incident.

**Cons:** Blunt — reboots healthy devices on schedule, and cannot respond to an acute degradation between scheduled runs. Doesn't actually *detect* the failure #9 is about; it sidesteps it.

#### A2: Reactive only

**Description:** Detect degradation → escalate → reboot; no scheduler.

**Pros:** Precisely targets the incident; only reboots when genuinely needed.

**Cons:** Heaviest build (probe loop, new predicate, escalation gating) with real unknowns (probe reach, device-IP source). Nothing ships until all of it lands. No cheap backstop.

#### A3 (Chosen): Both, phased — proactive Phase 1, reactive Phase 2+

**Description:** One ADR, one `reboot:` config surface, one `Rebooter` + audit table. Proactive scheduling ships first (small, builds on ADR-0005); reactive escalation follows as later phases reusing the same machinery.

**Pros:** Honors the issue's "recommend both." Operator gets a high-value, near-zero-config backstop immediately (Phase 1) without waiting on the probe's unknowns. Shared config/audit/cooldown surface avoids the churn ADR-0004 warned about when splitting related features. The two compose: proactive prevents most rot; reactive catches what slips through between schedules.

**Cons:** Larger ADR than either alone. Two behaviors operators must understand. Phase 2's probe carries unresolved questions (mitigated by verify-first gating, below).

### Fork B — reactive trigger signal

#### B1: Passive retry-% only

**Description:** Reuse `ClientSnapshot.tx_retries / wifi_tx_attempts` with a new signal-*decoupled* predicate (high retry regardless of RSSI).

**Rejected because:** it provably misses the motivating incident. The Fridge's retry-% may have looked unremarkable while its *packet loss* was 62%; passive AP-side retry counts approximate "is it passing packets," they don't measure it. Retry-% is also noisy under airtime contention. Building the reboot trigger — the most disruptive action the daemon has — on the weakest signal is the wrong trade.

#### B2: Active reachability probe only

**Description:** A probe loop pings / HTTP-GETs each eligible device's IP, measuring loss%/latency over a window; trip the trigger on sustained high loss.

**Pros:** Decisive — this is exactly what exposed the Fridge (62% ping loss while RSSI looked fine). Measures the thing that matters directly.

**Cons:** A new capability: needs cross-VLAN reach to device IPs, a device-IP source (`ClientSnapshot` lacks it), and a probe cadence that doesn't itself congest the air. No corroboration if the probe path is itself flaky.

#### B3 (Chosen): Active probe as trigger + passive corroboration

**Description:** The active probe (B2) is the trigger; passive retry-% and (optionally) UniFi `satisfaction` corroborate, and the **survives-a-reconnect gate** (`kick_no_roam` after a kick) is the empirical discriminator between a stuck *session* (clears on reconnect) and a rotted *stack* (survives it). The issue's own recommended rule:

> sustained high loss (active probe) **AND** RSSI adequate (not a distance problem) **AND** still bad after a reconnect/roam (`kick_no_roam`) → reboot.

**Pros:** Most robust; decouples loss from signal (the core insight) and confirms re-roaming can't help before escalating. Corroboration reduces false positives from a flaky probe path.

**Cons:** Most to build. Carries B2's probe unknowns. Adding `satisfaction` to `ClientSnapshot` is a (small) Protocol surface change.

## Decision

> **Implementation note (Phase 1, PR #11):** the `Rebooter` Protocol and scheduler shipped in `src/wifi_shepard/reboot/`, not a new `remediators/` package as written below — for consistency with ADR-0005's already-landed `reboot/` modules (`eligibility.py`, `ha_resolver.py`). Read the `remediators/` references throughout this ADR as `reboot/`.

**Chosen:** A3 (Both, phased) + B3 (active probe + passive corroboration), with the `Rebooter` Protocol in a new `remediators/` package and all cooldown/audit/dry-run machinery reused from ADR-0004/0005/0002.

**Rationale:**

1. **Phasing serves "simple to set up" without dodging the problem.** Proactive scheduling (Phase 1) is a thin layer over ADR-0005's resolver — opt-in MACs already exist, "how to reboot" is already resolved, so Phase 1 adds only a scheduler, the `Rebooter`, the audit table, dry-run, and a cooldown. It ships value immediately and would have prevented the incident. Reactive escalation (Phase 2+) then handles the acute case, reusing the same surface.
2. **The reactive trigger must not be built on retry-% alone** because that signal demonstrably misses the motivating incident (good RSSI, high loss). The active probe is the only signal that measured the failure; corroboration + the survives-a-reconnect gate keep it from firing on transients or a flaky probe path.
3. **`remediators/` keeps the architecture honest.** A reboot is not a controller action; isolating `Rebooter` from `controllers/` keeps the "scanner/scorer/actor don't know the vendor" boundary intact and gives future reboot backends (direct WLED HTTP, UniFi PDU outlet) a home without touching `controllers/`.
4. **Reuse over reinvention.** Cooldown/daily-cap = the ADR-0004 `KickRateLimiter` pattern; audit = the `kick_events` → `reboot_events` pattern; dry-run = the `would_kick` → `would_reboot` pattern; escalation gate = the existing `quarantine` / `kick_no_roam` state. Almost nothing here is a novel mechanism; it's the kick pipeline's shapes applied to a second action.
5. **Conservative by construction.** Opt-in + allowlist-absolute + escalation-only + dry-run-first + cooldown + daily-cap make the most disruptive action the daemon has safe to ship and tune, consistent with the ADR-0001/-0003/-0004 default-off discipline.

**Implementation forks resolved by this ADR:**

- **Model (Fork A):** Both, phased — proactive Phase 1, reactive Phase 2+. One ADR, one config block.
- **Reactive trigger (Fork B):** Active reachability probe as trigger; passive retry-% / `satisfaction` corroborate; `kick_no_roam`-survives-reconnect is the escalation gate. **"Adequate signal" reuses the existing ADR-0001 tunable** — it means `signal > detection.signal_dbm_max` (the inverse of the bad-state predicate's signal clause), not a new threshold. The escalation gate counts `after_failed_kicks` `kick_no_roam` outcomes (the quarantine boundary), so the count is a single configured knob, not a vague "prior state."
- **`Rebooter` location (Fork C):** New `src/wifi_shepard/remediators/` package (`base.py` Protocol + HA-backed impl that consumes ADR-0005's resolver). Not under `controllers/`.
- **`ClientSnapshot` extension (Fork D):** Add `satisfaction: int | None = None` (optional, best-effort; backends that don't expose it leave it `None`). The probe's **loss%** is **not** a snapshot field — it is owned by the probe loop and fed to the degradation predicate separately (the snapshot is controller-sourced; loss is daemon-measured).
- **Device-IP source for the probe (Fork E):** Open — resolved in Phase 0. Candidates: add `ip` to `ClientSnapshot` (controller already knows it), or resolve via the HA device. Defaulting to controller-sourced `ip` on the snapshot, pending Phase 0 verification of cross-VLAN reach.
- **Cooldown/cap state (Fork F):** In-memory on the `Rebooter`/actor, `time.monotonic`-based, injected `now_fn` for tests — identical posture to ADR-0004. Restart loses state; bounded by daily cap.
- **Audit (Fork G):** New `reboot_events` table (`mac`, `ts`, `mode ∈ {proactive, reactive}`, `outcome`, `target`, `dry_run`), `db.insert_reboot`. Owns the schema bump + UI sidecar coordination (ADR-0002). Surfaced in the read-only UI history.
- **Deferred-by-cooldown handling (Fork H):** Drop + log `reboot_deferred` (reason ∈ {cooldown, daily_cap}); no queue. Next scheduled run / next scan cycle re-evaluates — same as ADR-0004's `kick_deferred`.

**Config (sketch — extends ADR-0005's `reboot:` block):**

```yaml
reboot:
  enabled: true
  resolver: home_assistant          # from ADR-0005
  dry_run: true                     # log would_reboot only (like would_kick)
  eligible:                         # from ADR-0005 (opt-in MACs)
    - 08:f9:e0:ba:c4:84
    - 08:f9:e0:ba:c6:48
  cooldown:
    per_device_seconds: 3600        # never reboot-loop a device
    max_per_device_per_day: 4
  proactive:                        # Phase 1
    enabled: true
    schedule: "03:30"               # daily local time, HH:MM (fail-closed validated)
  reactive:                         # Phase 2+
    enabled: false                  # ships off; opt in after the probe is validated
    probe:
      method: ping                  # ping | http
      interval_seconds: 60
      window_samples: 5
      loss_pct_min: 30              # sustained loss to trip
    require_signal_adequate: true   # decouple from a weak-clinger (RSSI ok)
    after_failed_kicks: 2           # quarantine / kick_no_roam count before escalating
```

## Acceptance Criteria

- [ ] **AC-1**: Given `reboot.proactive.enabled: true`, `schedule: "03:30"`, an eligible MAC with a resolvable ADR-0005 target, and `dry_run: false`, when the daemon's clock reaches 03:30, then the `Rebooter` is invoked exactly once for that MAC, a `reboot_events` row is written with `mode='proactive'`, and a structured log line is emitted.
- [ ] **AC-2**: Given `reboot.dry_run: true` and a proactive schedule due, when the schedule fires, then a `would_reboot` log line is emitted for each eligible MAC, **no** `Rebooter` network call is made, and a `reboot_events` row is written with `dry_run=true` (audit symmetry with the fired path, matching [ADR-0004 AC-6](./0004-kick-rate-limits.md)).
- [ ] **AC-3**: Given `cooldown.per_device_seconds: 3600` and a MAC rebooted at `t`, when a proactive **or** reactive reboot for the same MAC is attempted before `t + 3600`, then it is deferred — a `reboot_deferred` line with `reason='cooldown'` and `retry_after_seconds` is logged, no `Rebooter` call is made, and no `reboot_events` (fired) row is written.
- [ ] **AC-4**: Given `cooldown.max_per_device_per_day: 4` and a MAC already rebooted 4 times today, when a 5th reboot is attempted, then it is deferred with `reason='daily_cap'`.
- [ ] **AC-5**: Given a MAC in `allowlist:` (or not in `reboot.eligible:`), when either a proactive schedule fires or a reactive trigger trips, then **no** reboot occurs for that MAC under any path.
- [ ] **AC-6**: Given `reboot.reactive.enabled: true`, an eligible MAC whose active probe shows sustained loss ≥ `loss_pct_min` over `window_samples`, **adequate signal** (`signal > detection.signal_dbm_max` — the inverse of the bad-state predicate's signal clause, so not a weak-clinger), **and** at least `after_failed_kicks` prior `kick_no_roam` outcomes (i.e. quarantine reached), when the reactive evaluation runs, then the degradation predicate trips and a reactive reboot is attempted (subject to cooldown/cap).
- [ ] **AC-7**: Given the same as AC-6 but with **only a single** high-loss probe sample (window not satisfied), when evaluation runs, then the predicate does **not** trip — no reboot.
- [ ] **AC-8**: Given an eligible MAC that is degraded but has **not** been through the re-roam ladder (no kick / no `kick_no_roam` yet), when reactive evaluation runs, then it does **not** reboot — escalation-only; a reboot is never the first action.
- [ ] **AC-9**: Given a weak-clinger (low RSSI, high loss) that recovers after a kick/roam (no `kick_no_roam`), when reactive evaluation runs, then it does **not** escalate to reboot — re-roaming handled it (the survives-a-reconnect discriminator).
- [ ] **AC-10**: Given `reboot.enabled: false` (or no `reboot:` block), when the daemon runs, then no scheduler task and no probe task start, no `reboot_events` rows are written, and behavior is identical to the pre-reboot baseline.
- [ ] **AC-11**: Given an invalid `reboot:` shape (e.g. non-`HH:MM` `schedule`, negative `cooldown.*`, unknown `probe.method`), when the daemon loads config, then it raises an error naming the field and exits — fail-closed, matching [ADR-0004 AC-7](./0004-kick-rate-limits.md).
- [ ] **AC-12**: Given a SIGHUP that changes `proactive.schedule` or `cooldown.*`, when the daemon reloads, then the new values take effect on the next evaluation while in-memory cooldown state (last-reboot timestamps) is **not** purged (ADR-0004 reload posture).

## Consequences

### Positive

- A high-value, near-zero-config backstop ships in Phase 1: opt-in MACs + a schedule, reusing ADR-0005's resolver. Prevents the rot the incident was about.
- The reactive path (Phase 2+) targets the acute case precisely, on the signal that actually measured the failure, and only after re-roaming has provably failed.
- `Rebooter` in `remediators/` keeps the controller boundary clean and gives future reboot backends a home.
- Reuses ADR-0004/0005/0002 machinery (cooldown, dry-run, audit, escalation seam) — little novel mechanism, low conceptual surface for operators already used to the kick pipeline.
- Conservative by construction (opt-in, allowlist-absolute, escalation-only, dry-run-first, cooldown, daily-cap).

### Negative

- Largest ADR in the set; two behaviors to document and tune.
- The active probe is a genuinely new capability with unresolved reach/IP-source questions — Phase 2 cannot be hand-waved.
- `reboot_events` is a schema bump requiring UI-sidecar coordination (ADR-0002).
- Proactive reboots restart healthy devices on schedule (mitigated: opt-in only, dry-run-first, daily-cap).
- Hard dependency on ADR-0005 (and its concrete-HA-client dependency) landing first.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Probe can't reach device IP (cross-VLAN) or `ClientSnapshot` has no IP | Medium | High | Phase 0 verify-first spike resolves the IP source (Fork E) and reach before Phase 2 code. Reactive ships **off** (`reactive.enabled: false`); proactive (Phase 1) needs no probe. |
| Reactive false-positive reboots a healthy device | Medium | High | Window-based probe (AC-7), signal-adequate gate, survives-a-reconnect gate (AC-9), escalation-only (AC-8), dry-run-first, per-device cooldown + daily cap. |
| Reboot-loop on a device that never recovers | Low | High | Per-device cooldown (AC-3) + daily cap (AC-4); after the cap, defer + log, never loop. |
| Proactive schedule reboots devices mid-use (e.g. LedFx show running) | Medium | Medium | Opt-in only; operator picks an off-hours `schedule`; dry-run to preview. A future quiet-hours / "in-use" guard is an anticipated follow-up. |
| Schema bump desyncs the UI sidecar | Low | Medium | Coordinate `reboot_events` migration with the sidecar bump, as ADR-0004 did for its schema interactions. |
| Adding `satisfaction` to `ClientSnapshot` breaks a backend that doesn't provide it | Low | Low | Optional field defaulting to `None`; backends populate best-effort; predicate treats `None` as "no corroboration," never as degraded. |

## Implementation Plan

Gated on ADR-0005's resolver existing. Build order: schema/protocol → proactive (ships value) → probe (verify-first) → reactive predicate/gating → docs.

- [ ] **Phase 0 — verify-first spike (no production code):** Confirm the device-IP source (Fork E — does the controller expose client IP cleanly enough to add `ClientSnapshot.ip`?) and that the daemon can reach an IoT-VLAN device IP to probe it. Decide `ping` vs `http` probe default. Output gates Phase 2.
- [ ] **Phase 1 — proactive (ships value):**
  - `src/wifi_shepard/remediators/base.py` — `Rebooter` Protocol; `remediators/ha.py` — impl consuming ADR-0005's resolver.
  - `db.py` — `reboot_events` table + `insert_reboot` (owns the schema bump + sidecar coordination).
  - `config.py` — `RebootScheduleConfig`, `RebootCooldownConfig`; fail-closed validation (AC-11); default-off (AC-10).
  - Scheduler task in `main.py` / a small `reboot/scheduler.py`; cooldown via an ADR-0004-style limiter (`now_fn` injected); `dry_run` → `would_reboot`.
  - Tests: `tests/test_reboot_proactive_ac1_ac2.py`, `tests/test_reboot_cooldown_ac3_ac4.py`, `tests/test_reboot_allowlist_ac5.py`, `tests/test_reboot_config_ac10_ac11.py`, `tests/test_reboot_sighup_ac12.py`.
- [ ] **Phase 2 — active probe loop:** `reboot/probe.py` — async probe task measuring per-device loss%/latency over a window; `ClientSnapshot.satisfaction` added (Fork D) and `ip` per Phase 0; feeds results to the predicate. Tests with a fake transport: `tests/test_reboot_probe.py`.
- [ ] **Phase 3 — reactive predicate + escalation gating:** new degradation predicate (signal-decoupled, window-based, corroborated) in `scorer.py` or a sibling; escalation gate keyed on quarantine / `kick_no_roam` (AC-6–AC-9); wired into the actor's escalation path. Reactive ships **off** by default. Tests: `tests/test_reboot_reactive_ac6_ac7.py`, `tests/test_reboot_escalation_ac8_ac9.py`.
- [ ] **Phase 4 — docs:** extend `config.example.yaml`'s `reboot:` block (proactive + reactive sketches), README runbook (recommend dry-run + a ≥1-week observe period before `reactive.enabled: true`), and prune ADR-0005's "anticipated follow-up" note now that this ADR exists.

## Related ADRs

- [ADR-0005](./0005-device-identification-and-reboot-backend.md) — resolves *how* to reboot (HA-delegated target + opt-in `eligible:` + override fallback); this ADR consumes that resolver and decides *when*.
- [ADR-0004](./0004-kick-rate-limits.md) — the cooldown / windowed-cap / `now_fn`-injection / deferred-and-log pattern this ADR reuses for reboot cooldown + daily cap.
- [ADR-0003](./0003-kick-mechanism-upgrade.md) — `kick_no_roam` and the quarantine state that form the reactive escalation gate (re-roam must fail before reboot).
- [ADR-0001](./0001-mvp-scope-base-feature.md) — per-MAC backoff/quarantine, `dry_run` posture, allowlist, and fail-closed config this ADR extends to the reboot action.
- [ADR-0002](./0002-device-history-and-status-ui.md) — the read-only UI history `reboot_events` is surfaced in; owns the coordinated schema bump.

Anticipated follow-ups (not yet written):

- **ADR for fingerprint-assist** — optional active probing (WLED `/json/info`, ESPHome mDNS, Tasmota status) to auto-suggest a reboot target for operator confirmation (carried over from ADR-0005).
- **ADR for quiet-hours / in-use guard** — suppress proactive reboots while a device is actively in use (e.g. a running LedFx show), beyond a static off-hours `schedule`.
- **ADR for direct reboot backends** — WLED HTTP (`{"rb":true}`) and UniFi PDU outlet power-cycle as `Rebooter` implementations alongside the HA-delegated one, for devices not in HA.

## References

- [Issue #9](https://github.com/ljmerza/wifi-shepard/issues/9) — feature proposal; design addendum #2 ("how does it know the Wi-Fi *stack* is bad vs a weak clinger") is the basis for the B3 trigger and the survives-a-reconnect discriminator. Incident postmortem referenced therein: `incidents/2026-05-24_wled-stutter.md`.
- [`PLAN.md`](../../PLAN.md) §4 (backoff schedule, per-day caps), §12 (safety rails).
- [`CLAUDE.md`](../../CLAUDE.md) — "don't push vendor specifics into the scorer" (the degradation predicate is brand-agnostic; the `Rebooter` lives in `remediators/`, not the scorer).
- [Python `asyncio`](https://docs.python.org/3/library/asyncio.html) — the scheduler and probe are event-loop tasks, not separate processes (ADR-0001 single-loop constraint).
