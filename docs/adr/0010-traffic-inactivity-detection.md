# ADR-0010: Traffic-Inactivity Detection — an Opt-in "Associated but No Traffic" Flatline Detector

**Status:** Accepted
**Date:** 2026-07-15
**Author:** Leonardo Merza

## Context

### Background

On 2026-07-15 two Meross msg100 garage openers (NoT VLAN, 2.4 GHz) lost their cloud MQTT sessions for days. The Meross app showed both offline; local control kept working. wifi-shepard sampled both every minute the whole time and never acted — its last kick had been June 24. A manual UniFi deauth/reconnect fixed one unit instantly (the cloud MQTT flow re-established within seconds of rejoining); the other needed a power cycle.

The root cause is structural. `scorer.is_bad_state()` is **conjunctive**: it flags a client only when **every** active client criterion fails on **every** sample in the window — weak `signal` **and** low `tx_rate` **and** high `retry_pct`, plus the radio filter and the ADR-0008 AP-saturation gate. With the shipped config (`signal_dbm_max: -70`, `tx_rate_kbps_max: 12000`, `retry_pct_max: 30`, `ap_cu_total_min: 60`), a client must be weak AND slow AND retrying AND on a saturated AP for the whole window.

State-DB evidence for one unit over the preceding 7 days:

- 4,843 / 4,849 samples below 12 Mbps (parked at 1 Mbps);
- 1,240 of those also had `ap_cu_total >= 60`;
- **20 separate runs of ≥5 consecutive samples** passing the rate + saturation gates;
- every one vetoed by the signal check — the unit sat at −45…−54 dBm all week.

A strong-signal-but-wedged client can never qualify, no matter how pathological the rate. The actual failure signature was application-layer: pristine WiFi telemetry, dead cloud session.

### Current State

- **`scorer.is_bad_state`** hard-`AND`s the three client criteria (ADR-0009 made each disable-able via `null`, but the ones left active still all-`AND`). There is no path that acts on "the WiFi looks fine but the client is doing nothing."
- **`ClientSnapshot`** already carries the per-sample WiFi telemetry; the UniFi backend fetches the raw client record each poll, which also contains cumulative `tx_bytes` / `rx_bytes` counters we did not previously surface.
- **`Actor.handle`** is the single choke point for every action — dry-run gate, per-MAC backoff (ADR-0007), hourly/daily caps, the ADR-0004 rate limits, and HA notification all live behind it, keyed off `client.mac` and an opaque decision object.
- **AP-saturation gate (ADR-0008)** and **quiet-hours tightening (ADR-0007)** are gates *on the conjunctive scorer* — exactly the gates that hid this failure.

### Requirements

1. **Detect the blind spot** — flag a client that is associated with healthy telemetry but is not passing traffic, which the conjunctive scorer structurally cannot.
2. **Do not thrash-kick healthy IoT** — these msg100s idle at 1 Mbps as legitimate power-save behavior; a naive rate-only rule would re-roam working garage doors every time their AP crosses the saturation floor.
3. **Reuse the action machinery** — a flag must flow through `Actor.handle` so dry-run, backoff, caps, rate-limits, allowlist, and notifications apply unchanged.
4. **Fail safe** — missing counters, or a counter reset from a reassociation/reboot, must never produce a spurious flag.
5. **Match the config house style** — a new `detection.inactivity:` block, fail-closed validation, per-MAC opt-in, shipped off.

### Constraints

One process, asyncio, pure decision logic; per-MAC state that survives SIGHUP where sensible (mirroring the ADR-0004/0009 reload convention). No new controller round-trips — the byte counters ride along on the existing per-poll client fetch.

## Options Considered

### Option 1: Naive fix — OR-semantics / `signal_dbm_max: null`

Relax the conjunction (make the criteria OR) or disable the signal criterion so a slow client alone qualifies.

**Pros:** no new code; expressible today via ADR-0009 `null`.
**Cons:** directly violates Req 2. One msg100 reports `tx_rate_kbps: 1000` right now with a healthy, *verified* cloud session — rate-only detection would thrash-kick a working device whenever its AP saturates. The AND semantics accidentally prevented exactly this. Rejected.

### Option 2: New independent detection class — "associated but no traffic" (Chosen)

Add a second, parallel detector alongside the conjunctive scorer: for an explicitly opted-in MAC, watch its cumulative `tx_bytes + rx_bytes` and flag when the summed delta over a full window is below a floor. Independent of signal/rate/retry, the saturation gate, and quiet hours. Dispatch through `Actor.handle` like any other kick.

**Pros:** models the real failure (traffic presence, not link quality); leaves the conjunctive scorer and its healthy-IoT protection untouched; opt-in keeps the blast radius to devices the operator asserts should be chatty; byte counters are already on the wire, so no extra controller calls; reuses every downstream safety gate for free.
**Cons:** byte counters approximate "WAN activity" — they include LAN traffic, so a device that is busy on the LAN but WAN-dead won't flag (see below); a floor + window is a coarse proxy for "has an established external session"; opt-in means it does nothing until the operator lists a MAC.

### Option 3: Baseline / profile learning

Learn each client's normal traffic profile ("this device holds a persistent WAN session") and flag deviations automatically, no opt-in.

**Pros:** no per-device configuration; adapts to the network.
**Cons:** the hard part, and easy to get wrong on a home-lab daemon — a mislearned baseline silently kicks healthy devices or silently protects wedged ones. It deserves its own ADR and its own evidence. Deferred, not rejected.

## Decision

**Chosen Option:** Option 2 — a new, independent, opt-in traffic-inactivity detection class.

**Rationale:** the incident was an application-layer stall behind perfect WiFi telemetry. No amount of tuning the link-quality criteria can catch that (Option 1 proved it, and would cause collateral kicks). A separate detector that watches *traffic presence* is the smallest change that addresses the failure without weakening the protection the conjunction provides for healthy power-save IoT. Routing it through `Actor.handle` means it inherits the entire ADR-0004/0007 safety envelope with no duplicated policy.

**Forks resolved:**

- **Byte counters as a WAN-activity approximation.** We flag on `tx_bytes + rx_bytes` deltas because those counters are already fetched per poll (no new API call). They count **all** client traffic, including LAN — so a device chatty on the LAN but dead to the WAN will not flag. This is a deliberate, documented approximation: it caught the msg100 case (which went fully silent) and is cheap. The higher-signal variant — DNS query patterns via Pi-hole, which distinguishes WAN intent — is the companion **issue #18** and will get its own ADR.
- **Opt-in, not learned.** v1 requires the operator to list MACs under `detection.inactivity.macs`, asserting "this device normally maintains a WAN session." Baseline learning (Option 3) is deferred. `enabled: true` with an empty `macs` list is legal but inert.
- **The saturation gate and quiet-hours tightening do NOT apply.** Those gates are scoped to the conjunctive scorer and are precisely what hid this failure. Applying them here would re-introduce the blind spot (a wedged device on an idle AP, or during quiet hours, would be spared). The inactivity detector is deliberately independent of `ap_cu_total_min` and the quiet-hours override thresholds.
- **Fail-safe semantics.** `None` counters (backend didn't report them) cannot be evaluated → clear the window, never flag. A negative delta (a counter reset when the client reassociates/reboots) counts as activity → clear the window. A flag only fires on a **full** window whose summed delta is below the floor; after flagging, the window is cleared so the MAC re-accumulates rather than re-flagging every poll (backoff/caps/rate-limits are the additional downstream protection).
- **Allowlist wins.** An allowlisted MAC is never actioned even if opted in; the scorer skips it (defense in depth) and config load emits an `inactivity_mac_in_allowlist` warning.
- **SIGHUP reload convention.** Mirrors the scorer: rebuild the inactivity scorer only when its own `window_samples` changes (deque `maxlen` is fixed at construction); any other change (`min_bytes_per_window`, `macs`, `enabled`) is read live, so a config swap-in-place preserves the accumulated byte windows.

## Acceptance Criteria

- **AC-1**: `detection.inactivity` parses with defaults (off) when absent — zero behavior change; invalid types (non-bool `enabled`), negative ints, `window_samples < 1` while enabled, and malformed MACs all fail closed with a clear `ValueError`.
- **AC-2**: The UniFi backend surfaces `tx_bytes` / `rx_bytes` on `ClientSnapshot` when present (fail-soft), and yields `None` when absent or malformed (including `bool`), without raising.
- **AC-3**: A `client_samples` table created before this ADR gains nullable `tx_bytes` / `rx_bytes` columns on connect (existing rows backfill to NULL), and new samples persist the counters.
- **AC-4**: An opted-in MAC whose summed byte delta over a full window is below `min_bytes_per_window` is flagged and reaches `Actor.handle`; under `dry_run: true` this produces the would-kick log path with no controller call.
- **AC-5**: A MAC not listed in `macs` is never flagged regardless of its deltas; an allowlisted MAC is never flagged even when opted in, and config load warns (`inactivity_mac_in_allowlist`).
- **AC-6**: A counter reset (negative delta) or `None` counters clears the window — no flag.
- **AC-7**: Existing detection is untouched: the full pre-existing suite stays green, and an inactivity flag fires even when `ap_cu_total` is below `ap_cu_total_min` (no saturation gate on this class).
- **AC-8**: A SIGHUP-style `update_config` picks up changed inactivity settings — a `window_samples` change rebuilds the scorer; a `min_bytes_per_window` (or `macs`/`enabled`) change applies in place, preserving accumulated per-MAC state.

## Consequences

### Positive

- Catches the application-layer stall class the conjunctive scorer structurally cannot see, using data already on the wire.
- Zero change to the conjunctive scorer — healthy power-save IoT keeps its AND-based protection; the new detector only touches MACs the operator opted in.
- Inherits the entire ADR-0004/0007 safety envelope (dry-run, backoff, caps, rate-limits, allowlist, notifications) by dispatching through `Actor.handle`.

### Negative

- Byte counters include LAN traffic, so a WAN-dead-but-LAN-chatty device is missed (issue #18 / DNS variant addresses this).
- Opt-in means no protection until the operator lists a MAC; no baseline learning in v1.
- A device that legitimately transmits nothing for a full window (truly idle, no keepalive) could be flagged — mitigated by opt-in scope, the floor/window sizing, dry-run, and the downstream caps/backoff.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Operator opts in a device that legitimately idles with no keepalive → spurious kicks | Medium | Low | Opt-in only; `dry_run` first; ADR-0007 caps/backoff/quarantine; tune `min_bytes_per_window` / `window_samples` |
| LAN-only chatter masks a WAN stall → missed detection | Medium | Low | Documented approximation; DNS-based variant tracked in issue #18 |
| Counter reset misread as a flatline | Low | Medium | Negative delta treated as activity, clears the window |
| Missing counters misread as a flatline | Low | Medium | `None` counters clear the window and never flag (fail-safe) |
| Opted-in MAC also allowlisted | Low | Low | Scorer skips allowlisted MACs; load-time `inactivity_mac_in_allowlist` warning |

## Implementation Plan

- [x] **ClientSnapshot / UniFi** — add fail-soft `tx_bytes` / `rx_bytes`; UniFi fills them via `_optional_int` (keeps only a real `int`, never `bool`), never `_require`.
- [x] **DB** — extend `_CLIENT_SAMPLES_MIGRATIONS` with nullable `tx_bytes` / `rx_bytes`; persist them in `insert_sample`.
- [x] **Config** — `InactivityConfig` (`enabled` / `min_bytes_per_window` / `window_samples` / `macs`) hung off `DetectionConfig`; `_build_inactivity` fail-closed validation; `inactivity_mac_in_allowlist` load warning.
- [x] **Scorer** — new `inactivity.InactivityScorer`: opt-in gate, allowlist skip, per-MAC prev-total + delta deque, fail-safe on `None`/negative deltas, clear-after-flag.
- [x] **Pipeline / Scanner** — add the inactivity scorer to `DetectionPipeline` + `build_pipeline`; own-`window_samples` reload convention; scanner feeds each client and dispatches a flag through `Actor.handle`.
- [x] **Tests** — `tests/test_inactivity_ac{1..8}.py`; existing suite stays green.
- [x] **Docs** — index row; `config.example.yaml` `detection.inactivity:` block + a note that the three client criteria + saturation gate combine conjunctively.

## Related ADRs

- [ADR-0009](./0009-disable-able-detection-criteria.md) — made the conjunctive criteria disable-able; this adds an orthogonal detector rather than another criterion in the same `AND`.
- [ADR-0008](./0008-ap-saturation-gate.md) — the AP-saturation gate that (correctly, for its class) helped hide this failure; intentionally not applied here.
- [ADR-0007](./0007-action-policy-backoff-and-quiet-hours.md) — the backoff / caps / quiet-hours action policy this detector dispatches into; quiet-hours tightening intentionally not applied here.
- [ADR-0004](./0004-kick-rate-limits.md) — the rate limits and the reload-in-place convention this detector's SIGHUP handling mirrors.

## References

- [`PLAN.md`](../../PLAN.md) §3 — detection criteria.
- GitHub issue #17 (this ADR); issue #18 — the DNS-based WAN-activity companion.
- `src/wifi_shepard/inactivity.py`, `src/wifi_shepard/config.py`, `src/wifi_shepard/scanner.py`, `src/wifi_shepard/pipeline.py`, `src/wifi_shepard/controllers/{base,unifi}.py`, `src/wifi_shepard/db.py`.
