# ADR-0005: Device Identification & Reboot-Backend Selection — Delegate to Home Assistant

**Status:** Accepted
**Date:** 2026-05-24
**Author:** Leonardo Merza

## Context

### Background

[Issue #9](https://github.com/ljmerza/wifi-shepard/issues/9) proposes a **reboot-escalation** remediation. Today the daemon's only action is "kick / BTM the client so it re-roams to a better AP" — which fixes the **clinger / airtime-hog** failure. It does not fix a second failure class hit in production: an ESP32 whose **Wi-Fi stack has degraded after long uptime** — high packet loss *despite adequate signal* — which re-roaming cannot cure because the radio itself is sick. The only fix is a **power-cycle / reboot** of the device.

The incident behind the issue: three kitchen ESP32 WLEDs on 2.4 GHz. "Fridge" sat at a *healthy* −58 dBm yet dropped 62% of pings; re-roaming would not have helped, and a reboot restored it to 0% loss. So the daemon needs a reboot action — but a reboot is **not** an AP-controller action (UniFi cannot reboot a Wi-Fi *client*), and to reboot a device you must know *how* to reach and restart that specific device.

The issue's design addendum frames the question this ADR answers:

> "since we have to know what type of device it is **(do we?)** … set in config what type of device or maybe automatic with api queries and see what sticks?"

**The answer is split, and that split defines this ADR's scope:**

- **Detection does NOT need device type.** Whether a radio is sick is firmware-agnostic — it rides on existing `ClientSnapshot` signals plus (per the follow-up ADR) an active reachability probe. WLED, ESPHome, Tasmota, and custom sketches all run on the same Espressif chips, so MAC, signal, retries, and AP look identical in `ClientSnapshot`.
- **The *reboot step* DOES need device identity.** You must know which backend power-cycles *this* device. The MAC **OUI** reveals only the chip vendor (Espressif) — never the firmware. "WLED vs ESPHome vs smart-plug-behind-it" therefore **cannot be derived from controller data alone**; it has to come from somewhere outside the Wi-Fi/controller layer.

This ADR decides **only** how the daemon identifies a device and selects its reboot backend. The full detect → escalate → reboot feature — the degradation predicate, the `Rebooter` protocol surface, escalation gating, cooldowns/daily caps, dry-run, and the audit table — is deferred to a **separate follow-up ADR** (see Related ADRs).

### Current State

- **HA integration is a Protocol stub only.** `src/wifi_shepard/notify/__init__.py` defines a `Notifier` Protocol (`notify`, `close`); there is **no concrete Home Assistant REST client in the repo yet**, and `home_assistant:` is **not parsed from YAML** — `config.example.yaml:8-9` flags it as "HA notifier built/passed via `build_daemon` kwarg instead of constructed from YAML," i.e. injected and out of scope for the current PR. The example block (`config.example.yaml:31-34`) shows the intended shape (`url`, `token`, `notify_service`) but it is not wired. → Any HA-delegated reboot resolution **depends on a concrete HA client landing first**, and on extending that client beyond notify-only.
- **Per-MAC config precedent exists.** `allowlist:` (MACs never kicked) and `overrides:` (per-MAC threshold tweaks) already establish the operator's mental model. Resolution is **per-MAC override > global default** ([ADR-0001 AC-6](./0001-mvp-scope-base-feature.md), `resolution.py`, `config.py` `OverrideEntry`).
- **The override entry already accepts and silently drops a `name:` field.** `config.py` filters override dict keys down to the known dataclass fields (`{k: v for k, v in o.items() if k in known}`), so `config.example.yaml`'s `name: "leonardo s22"` is parsed and discarded. This is a precedented slot for per-MAC labels/metadata — useful for a human-readable reboot target.
- **`ClientSnapshot` carries no device identity.** `controllers/base.py`'s `ClientSnapshot` has `mac`, `signal`, `tx_rate_kbps`, `tx_retries`, `wifi_tx_attempts`, `radio`, `ap_id`, `ap_cu_total` — no vendor, OUI, hostname, model, or firmware field. Device classification is absent from the design today; `PLAN.md` §12 lists fingerprinting only as a v2+ brainstorm.

### Requirements

1. **Detection stays firmware-agnostic.** This ADR must not push any device-type knowledge into the scorer/scanner ([CLAUDE.md](../../CLAUDE.md): "don't push vendor specifics into the scorer"). Identification is consulted only when selecting a reboot backend, never when detecting degradation.
2. **Simple to set up.** Per-device reboot config must be near-zero for the common case. The operator should not hand-author a backend type + address/endpoint for every device.
3. **Opt-in, never opt-out.** A device is reboot-eligible only if the operator explicitly opted it in. A reboot is more disruptive than a kick; the daemon must never reboot a device it merely *guessed* was IoT.
4. **Allowlist is absolute.** A MAC in `allowlist:` is never reboot-eligible, regardless of OUI or HA presence.
5. **Fail safe on unresolved targets.** If the daemon cannot determine how to reboot an eligible device, it must log a clear warning and take **no** action — never fall back to a destructive guess.
6. **Explicit override beats auto-resolution.** Where the operator supplies an explicit reboot target for a MAC, that wins over any auto-resolved one, mirroring the existing override > default semantics.

### Constraints

- One Python process, one event loop ([ADR-0001 §Constraints](./0001-mvp-scope-base-feature.md)). Identification resolution happens off the hot scan path (only when a reboot is being considered), not per-poll for every client.
- No `Controller` Protocol changes. Reboot is a separate surface (the follow-up ADR's `Rebooter`), not a controller method — UniFi cannot reboot a client.
- Reuse the existing HA dependency rather than adding a new one. The project already commits to Home Assistant for notifications and ships an `HA_TOKEN` secret.
- **`ClientSnapshot` is not extended by this ADR.** Adding `satisfaction` or any identity field is out of scope; identification is sourced from HA / config, not from the controller snapshot.

> **Two assumptions this ADR records as "verify during implementation," not as established fact** (the author is not certain of current HA internals):
> 1. HA's **device & entity registry appears to be WebSocket-API only** — the REST `/api/` surface exposes states and services, not the registries. If so, MAC→device matching needs HA **WebSocket** access, a capability beyond today's intended REST-notify client. *Confirm before building the resolver.*
> 2. The WLED and ESPHome integrations are believed to register a `("mac", …)` **device connection** in the registry and to expose a **restart `button`** entity (device_class `restart`); a smart-plug power-cycle uses a `switch` entity. *Confirm exact entity shapes (and ESPHome's, which depends on the device's firmware exposing a restart button) before coding the reboot-path resolver.*

## Options Considered

The three identification strategies from the issue's design addendum, reframed as "how does the daemon learn which reboot backend a device uses."

### Option 1: Explicit per-MAC reboot config

**Description:** The operator declares, per MAC, the reboot backend and its address/endpoint — e.g. `MAC → {backend: wled, address: "http://10.10.40.118"}` or `{backend: ha, entity: "switch.kitchen_stove_plug"}`. This is the issue's original `targets:` map.

**Pros:**
- Fully reliable and deterministic; no network probing, no dependence on a device being in any other system.
- Matches the existing per-MAC `allowlist:` / `overrides:` pattern exactly.
- Works for devices not in Home Assistant.

**Cons:**
- Most operator typing: a backend type **and** an address/entity per device, kept in sync as IPs (often DHCP on an IoT VLAN) change.
- Re-implements classification the operator likely already did in Home Assistant.
- Fails Requirement 2 ("simple to set up") for the common homelab case where devices are already in HA.

### Option 2 (Chosen): Delegate identification to Home Assistant

**Description:** Identify the device by matching the degraded client's MAC against **Home Assistant's device registry**, then resolve that device's reboot path *within HA* (a restart `button`, else an associated power `switch`) and trigger it via HA `call_service`. The operator's per-device config shrinks to an **opt-in MAC list** — HA already classified these devices (via the WLED / ESPHome integrations) and already knows how to restart them.

**Pros:**
- Near-zero per-device config: the operator opts a MAC in; HA resolves *how* to reboot it. Satisfies Requirement 2 directly — this is the "simple to set up" win over the issue's `targets:` map.
- Reuses the HA dependency the project already commits to (notifications, `HA_TOKEN`). One integration, extended from notify-only to action.
- HA is the single source of truth for device classification — no duplicate vendor/firmware bookkeeping in wifi-shepard, and `ClientSnapshot` stays identity-free.
- Composes with the existing per-MAC override pattern for the long tail (Option 1 becomes the fallback, not the primary surface).

**Cons:**
- Couples reboot capability to HA availability and to the device actually being present in HA's registry.
- Needs HA **registry** access — likely the WebSocket API (see Constraints note 1), a capability beyond the intended REST-notify client. New integration surface to build and test.
- The reboot-path resolution within HA is not uniform (restart button vs smart-plug switch vs nothing) and depends on HA-internal entity shapes that must be verified (Constraints note 2).
- Depends on the concrete HA client landing first; this ADR records direction + acceptance criteria, not a same-PR build.

### Option 3: Active fingerprint probe

**Description:** Probe the client's IP to auto-detect the firmware and thus the backend — WLED `GET /json/info` (`"brand":"WLED"`), ESPHome mDNS `_esphomelib._tcp`, Tasmota `GET /cm?cmnd=Status`.

**Rejected as the primary mechanism because:** it requires the daemon to have cross-VLAN reach to each device IP, per-firmware probe logic, and tracking of (DHCP-mobile) IPs — and the probe specifics for ESPHome/Tasmota are unverified. It re-derives information HA already holds authoritatively. It is best kept as an **optional assist** (auto-suggest a target for the operator to confirm), layered on later, not the source of truth for an action as disruptive as a reboot.

### Option 4: OUI-only auto-classification

**Description:** Infer "this is a rebootable IoT device" from the MAC OUI (Espressif vendor block) and reboot accordingly.

**Rejected because:** the OUI identifies the **chip vendor only**, never the firmware (Requirement: WLED/ESPHome/Tasmota share Espressif OUIs) and never the reboot path. It also cannot distinguish a rebootable WLED from someone's Espressif-based project that must never be power-cycled. OUI is retained only as a **coarse pre-filter** ("is this even an Espressif/IoT candidate?") paired with the opt-in list and allowlist — never as an action trigger on its own.

## Decision

**Chosen Option:** Option 2 — delegate device identification and reboot-backend selection to Home Assistant, with explicit per-MAC config as the fallback and reboot eligibility strictly opt-in.

**Rationale:**

1. The "do we need to know the device type?" question resolves cleanly: **no for detection, yes for reboot** — and HA already holds the "yes for reboot" answer. Delegating to HA means wifi-shepard never builds or maintains a device-classification system, and `ClientSnapshot` stays identity-free, honoring the "don't push vendor specifics into the scorer" rule.
2. It is the simplest setup for the target environment. The operator already runs Home Assistant and already has these WLED/ESPHome devices in it; the per-device config collapses to "list the MACs you allow rebooting." HA resolves *how*.
3. It reuses an existing dependency instead of adding probing infrastructure (Option 3) or duplicating classification in YAML (Option 1).
4. It composes with the established per-MAC override pattern. Devices HA can't auto-resolve get an explicit override — the same mental model as `overrides:`, landing in the precedented per-MAC slot (and giving the silently-dropped `name:` field a first real use).
5. Opt-in + allowlist + fail-safe-on-unresolved makes an inherently disruptive action conservative by construction, consistent with the ADR-0001/-0003/-0004 conservative-default discipline.

**Resolution ladder (the decision, stated operationally):**

1. **Eligibility gate.** A MAC is reboot-eligible only if it is in the `reboot.eligible:` opt-in list **and not** in `allowlist:`. OUI is, at most, a coarse pre-filter to warn on obviously-non-IoT opt-ins; it never grants eligibility.
2. **Identification.** For an eligible, degraded MAC, look up the HA device whose registry **connections include that MAC**.
3. **Reboot-path resolution within HA.** Prefer a restart `button` on the matched device; else an associated power `switch` (power-cycle: off → wait → on). The exact entity-selection rules are an implementation detail to verify (Constraints note 2).
4. **Override.** An explicit per-MAC `ha_entity` (or, later, other backend) override **wins** over auto-resolution (override > auto).
5. **Fail safe.** If eligible but unresolved (not in HA, no suitable entity, no override), log a clear `reboot_target_unresolved` warning and take **no** action.

**Config (sketch — minimal form + override fallback):**

```yaml
reboot:
  enabled: true
  resolver: home_assistant      # match MAC -> HA device registry
  eligible:                     # opt-in MACs; HA resolves *how* to reboot
    - 08:f9:e0:ba:c4:84
    - 08:f9:e0:ba:c6:48
  overrides:                    # only for devices HA can't auto-resolve
    - mac: 08:f9:e0:ba:c6:48
      name: "kitchen stove wled"
      ha_entity: switch.kitchen_stove_plug   # explicit power-cycle target
```

Minimal setup = HA `url`/`token` (already needed for notifications) + the `eligible:` list. No per-device backend or address required when HA resolves it — that is the simplification this ADR buys versus the issue's original `targets:` map.

**Implementation forks resolved by this ADR:**

- **Identification source (Fork A):** Home Assistant device registry (match by MAC connection). Not OUI, not controller snapshot, not config-as-primary.
- **Eligibility model (Fork B):** Explicit per-MAC opt-in (`reboot.eligible:`), intersected with the allowlist (allowlist always wins). OUI is a pre-filter warning only, never an action trigger.
- **Fallback location (Fork C):** Explicit per-MAC override in a `reboot.overrides:` block, reusing the existing per-MAC override pattern and the precedented (currently-dropped) `name:` slot. Override > auto-resolution.
- **Unresolved handling (Fork D):** Fail safe — log `reboot_target_unresolved`, take no action. Never guess a destructive action.
- **Snapshot scope (Fork E):** `ClientSnapshot` is **not** extended for identity. No vendor/OUI/satisfaction field added by this ADR.
- **Scope boundary (Fork F):** This ADR stops at *identification + backend selection*. The `Rebooter` Protocol, reboot execution, cooldowns/daily caps, dry-run `would_reboot`, and the `reboot_events` audit table are the **follow-up ADR**'s decisions.
- **Fingerprint probe (Fork G):** Deferred. Active probing (Option 3) is, at most, a later optional *assist* that pre-fills/validates config; it is not part of this decision.

## Acceptance Criteria

- [ ] **AC-1**: Given a MAC listed in `reboot.eligible:` that is present in HA's device registry (its device connections include that MAC), when the daemon resolves the device's reboot backend, then it identifies the matching HA device and selects a reboot entity — a restart `button` if present, else an associated power `switch` — **without** any per-device backend/address declared in config.
- [ ] **AC-2**: Given a MAC in `reboot.eligible:` that is **not** resolvable in HA (no matching device, or no suitable reboot entity) and has **no** `reboot.overrides:` entry, when resolution runs, then the daemon logs a structured `reboot_target_unresolved` warning (with `mac`) and selects **no** reboot target — it never falls back to a guessed action.
- [ ] **AC-3**: Given a `reboot.overrides:` entry for MAC `X` with `ha_entity: switch.foo`, when MAC `X` is resolved, then the override target (`switch.foo`) is used and HA registry auto-resolution for `X` is **not** consulted; for any other eligible MAC, auto-resolution is used (override > auto, mirroring [ADR-0001 AC-6](./0001-mvp-scope-base-feature.md) threshold resolution).
- [ ] **AC-4**: Given a MAC present in `allowlist:`, when reboot eligibility is evaluated, then the MAC is **never** reboot-eligible — even if it also appears in `reboot.eligible:` and is resolvable in HA — and a config-load warning is emitted for the contradictory `allowlist` ∩ `eligible` entry.
- [ ] **AC-5**: Given only a MAC and its OUI (no `reboot.eligible:` entry), when eligibility is evaluated, then the device is **not** reboot-eligible and the system makes **no** firmware/backend assumption from the OUI (asserted by test: OUI alone yields neither eligibility nor a resolved backend).
- [ ] **AC-6**: Given `reboot.enabled: false` (or no `reboot:` block), when the daemon loads config and runs, then no reboot resolution is attempted for any MAC and behavior is identical to the pre-reboot baseline (default-off, matching the ADR-0004 conservative-default posture).
- [ ] **AC-7**: Given a `reboot:` block with an invalid shape (e.g. `eligible:` containing a non-MAC string, `resolver:` set to an unknown value, or an override missing both `mac` and a target), when the daemon loads config at startup, then it raises an error naming the offending field and exits — fail-closed, matching [ADR-0001 §Decision](./0001-mvp-scope-base-feature.md) and [ADR-0004 AC-7](./0004-kick-rate-limits.md).

> Acceptance criteria for reboot *execution*, dry-run `would_reboot`, per-device cooldown / daily cap, and the audit table are intentionally **out of scope** here and belong to the follow-up ADR. This ADR's ACs stop at *resolving who the device is and how it would be rebooted*.

## Consequences

### Positive

- Near-zero per-device setup for the common case: the operator lists eligible MACs and HA does the rest. Directly answers the "make this simple to set up" requirement.
- Detection stays firmware-agnostic; `ClientSnapshot` and the scorer are untouched. Device identity lives entirely in HA/config, never in the controller layer.
- Reuses the existing HA dependency — one integration extended from notify-only to action — instead of adding a probe engine or a parallel classification store.
- Conservative by construction: opt-in + allowlist-absolute + fail-safe-on-unresolved make an inherently disruptive action safe to ship behind `dry_run` (in the follow-up ADR).
- The explicit-override fallback gives the long-tail (devices not in HA) a path without making the common case pay for it, and finally uses the precedented `name:` slot.

### Negative

- Couples reboot capability to Home Assistant: a device not in HA's registry (or HA being down) yields `reboot_target_unresolved` and no action until an override is added.
- Requires extending the HA integration to read the device/entity **registry**, likely via the WebSocket API (Constraints note 1) — more than the intended REST-notify client. New surface to build, secure, and test.
- Reboot-path resolution within HA is non-uniform and depends on HA-internal entity shapes (restart button vs smart-plug switch) that must be verified before coding (Constraints note 2); resolution rules may need iteration.
- Hard dependency ordering: this work cannot land until a concrete HA client exists (currently only a `Notifier` Protocol stub).

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| HA registry is WebSocket-only and the WS client is non-trivial to add | Medium | Medium | Verify-first spike (Implementation Phase 0) before committing to the resolver. If WS proves heavy, an interim path is explicit `reboot.overrides:` only (Option 1 fallback) with HA auto-resolution deferred — the config surface already supports it. |
| WLED/ESPHome don't expose a uniform restart entity; resolution picks the wrong entity | Medium | High | Fail-safe-on-unresolved (AC-2) plus override-wins (AC-3) mean a mis-resolution is correctable by an explicit `ha_entity`. The follow-up ADR's `dry_run`/`would_reboot` lets operators verify the resolved target before any real power-cycle. |
| Operator opts in a non-IoT or critical device by MAC typo | Low | High | Allowlist is absolute (AC-4); OUI pre-filter warns on obviously-non-Espressif opt-ins (AC-5); reboot ships default-off (AC-6) and behind dry-run in the follow-up ADR. |
| Device IP/MAC churn (DHCP, MAC randomization) breaks HA matching | Low | Medium | Matching is on the registry MAC connection, not IP, so DHCP churn is irrelevant; randomized client MACs are an IoT-atypical case and surface as `reboot_target_unresolved` (no action) rather than a wrong reboot. |
| HA unreachable at resolution time | Medium | Low | Treated as unresolved → `reboot_target_unresolved`, no action (AC-2). Reboot is an escalation, never time-critical; the next cycle retries. |

## Implementation Plan

This ADR is design-only; no source code lands with it. The build sequence below is for the implementing PR (gated on a concrete HA client existing) and is the basis for `/adr-to-pr`.

- [ ] **Phase 0 — verify-first spike (no production code):** Confirm Constraints notes 1 & 2 against a live HA: (a) can the device registry be read, and via REST or only WebSocket; (b) do the project's WLED/ESPHome devices expose a MAC connection and a restart `button`, and what does a smart-plug `switch` reboot look like. Record findings; if WS is required, scope the WS client. Output gates the rest of the plan.
- [ ] **Phase 1 — config plumbing** (`src/wifi_shepard/config.py`): add a `RebootConfig` dataclass (`enabled`, `resolver`, `eligible: tuple[str, ...]`, `overrides: tuple[RebootOverride, ...]`) and a `RebootOverride` (`mac`, `name`, `ha_entity`). Parse the `reboot:` block; fail closed on invalid MACs, unknown `resolver`, or malformed overrides (AC-7). Default-off when absent (AC-6). Tests: `tests/test_config_reboot_ac6_ac7.py`.
- [ ] **Phase 2 — eligibility resolver** (`src/wifi_shepard/reboot/eligibility.py`, new package): pure function `is_reboot_eligible(mac, config) -> bool` enforcing `eligible ∩ ¬allowlist`, with the allowlist-wins warning and the OUI pre-filter warning. No I/O. Tests: `tests/test_reboot_eligibility_ac4_ac5.py` (AC-4, AC-5).
- [ ] **Phase 3 — HA backend resolver** (`src/wifi_shepard/reboot/ha_resolver.py`): given an eligible MAC, resolve a reboot target — explicit override first (AC-3), else HA registry match → reboot entity (AC-1), else `reboot_target_unresolved` (AC-2). Depends on the concrete HA client (Phase 0 outcome) and reuses its session/token. Tests with a fake HA registry transport: `tests/test_reboot_ha_resolver_ac1_ac2_ac3.py`.
- [ ] **Phase 4 — docs**: add a commented `reboot:` block to `config.example.yaml` (minimal `eligible:` form + an `overrides:` example), and a README note that this resolves *who/how* only — actual reboot execution arrives with the follow-up ADR and ships default-off behind dry-run.

## Related ADRs

- [ADR-0001](./0001-mvp-scope-base-feature.md) — defines the per-MAC override > global-default resolution this ADR mirrors for `reboot.overrides:` (AC-3), the `allowlist:` this ADR treats as absolute (AC-4), and the fail-closed config posture (AC-7).
- [ADR-0003](./0003-kick-mechanism-upgrade.md) — establishes "discovery by observation / don't pre-classify capabilities" and the conservative-default discipline this ADR follows for reboot eligibility.
- [ADR-0004](./0004-kick-rate-limits.md) — the cooldown / rate-limit pattern the **reboot-escalation follow-up ADR** will reuse for per-device reboot cooldowns and daily caps.

Anticipated follow-ups (not yet written):

- **ADR for reboot-escalation remediation** — the other half of Issue #9: the degradation predicate ("high loss, decent signal," confirmed via an active reachability probe and the `kick_no_roam` survives-a-reconnect gate), a `Rebooter` Protocol surface (sibling to `Controller`, likely under a `remediators/` package), the actor escalation path, per-device cooldown + daily cap, dry-run `would_reboot`, and a `reboot_events` audit table. Consumes this ADR's resolved reboot target.
- **ADR for active reachability probe** — a probe loop measuring per-device loss%/latency (the signal that exposed the 62%-loss "Fridge" at good RSSI), with the same cross-VLAN reachability question as the reboot backend. May or may not be folded into the escalation ADR.
- **ADR for fingerprint-assist (Option 3)** — optional active probing (WLED `/json/info`, ESPHome mDNS, Tasmota status) to auto-suggest a reboot target for operator confirmation; layered on top of this ADR, not replacing it.

## References

- [Issue #9](https://github.com/ljmerza/wifi-shepard/issues/9) — feature proposal and both design addenda (device identification & reboot-backend selection; "how does it know the Wi-Fi *stack* is bad vs a weak clinger"). Incident postmortem referenced therein: `incidents/2026-05-24_wled-stutter.md`.
- [`PLAN.md`](../../PLAN.md) §12 (device fingerprinting listed as v2+ brainstorm, not committed).
- [`CLAUDE.md`](../../CLAUDE.md) — "don't push vendor specifics into the scorer" (device identity is sourced from HA/config, never the controller snapshot).
- [Home Assistant device registry](https://developers.home-assistant.io/docs/device_registry_index/) — device `connections` (including MAC) used for the match; access surface (REST vs WebSocket) to be confirmed in Phase 0.
