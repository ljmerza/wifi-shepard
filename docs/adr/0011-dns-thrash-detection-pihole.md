# ADR-0011: DNS-Thrash Detection via an Optional Pi-hole Data Source

**Status:** Accepted
**Date:** 2026-07-15
**Author:** Leonardo Merza

> **Amendment (2026-07-16):** the shared per-source password is now the *optional*
> fallback for **per-instance passwords** — each `dns_sources[].instances[]` may set its
> own `password: ${VAR}`, resolved as `instance password → else source password`. Every
> instance must still resolve to a non-empty password (fail-closed). Backward compatible:
> a source-level password with password-less instances behaves exactly as before.

## Context

### Background

On 2026-07-15 a Meross msg100 smart plug whose cloud MQTT session had wedged spent days in a tight application-layer reconnect loop: **478 of its last 500 DNS queries were for the same broker hostname** (`mqtt-us-4.meross.com`), roughly one resolve every two minutes, around the clock. Its WiFi telemetry was pristine the entire time — good signal, normal tx-rate, low retries — so nothing in the existing detection model (`scorer.is_bad_state`, ADR-0009's disable-able criteria, ADR-0008's AP-saturation gate) could see it. After a manual kick fixed the unit it resolved the broker exactly twice and went quiet; the before/after contrast is stark.

DNS-thrashing is a **high-signal, low-false-positive** indicator of an application-layer reconnect loop:

- a healthy client with an established persistent session re-resolves its endpoint rarely (a TTL refresh at most);
- a wedged client re-resolves the same name on every failed connect attempt.

This is a different failure class from the RF-layer badness the scorer already catches, and it is orthogonal — a device can be RF-healthy and application-wedged at once. It deserves its own signal feeding the *same* remediation pipeline.

### Current State

- **`Controller`** (controllers/base.py) is the only external data surface, and the UniFi controller **does not expose per-client DNS query logs**. There is no place in the model for a DNS signal to enter.
- **`ClientSnapshot`** carries `mac`, RF metrics, `ap_id`, `ap_cu_total`, `name` — but no `ip`, so there is nothing to join a per-IP DNS view against.
- **The scorer → actor pipeline** (dry-run gate, ADR-0007 backoff/caps/quiet-hours, ADR-0004 rate limits, HA notify) is exactly the remediation a DNS-flagged client wants; it is keyed off a `ClientSnapshot` + a resolved-thresholds dict passed to `Actor.handle`.
- **Config** is fail-closed YAML with `${VAR}` interpolation and `_SECRET_KEYS` redaction (`password`/`token` masked at any depth in error messages).

### Requirements

1. **See the thrash** — count per-client resolutions of a single domain and flag a sustained loop, without the UniFi controller (which can't see DNS).
2. **Additive, not a rewrite** — a DNS flag must route through the existing `Actor.handle`, inheriting dry-run, backoff, caps, quiet-hours, rate-limits, and HA notification unchanged. No parallel kick path.
3. **Optional and off by default** — absent config → zero behavior change; the existing suite stays green and shipped configs are unchanged.
4. **Fail-soft at runtime, fail-closed at config** — a down data source must never break the scan loop (the RF signal keeps working); a *misconfigured* detection the operator believes is armed must fail loudly at load.
5. **Two resolvers** — this network runs two Pi-hole instances and a client may use either, so a client's queries can land entirely on one; the source must merge instances.

### Constraints

One process, asyncio, single event loop. Match `config.py`'s fail-closed validation helpers and the `Controller`/`Notifier` Protocol style. Secrets stay in env vars (`${PIHOLE_PASSWORD}`), never in the repo, never in an error message.

## Options Considered

### Option 1: UniFi Traffic Flows API

UniFi exposes a Traffic Flows / DPI view that includes DNS.

**Pros:** no new dependency or backend; reuses the existing controller session.
**Cons:** it is **heavily aggregated and sampled** — the same 2026-07-15 incident that produced ~478 actual queries showed only **~4 DNS flow records**. Rate detection needs the raw event stream, not a rolled-up summary; 4 records can't distinguish "resolved twice, healthy" from "resolved 478 times, wedged". Disqualifying for this signal.

### Option 2: Pi-hole v6 FTL REST API as an optional data-source backend (Chosen)

Add a brand-agnostic `DnsSource` Protocol alongside `Controller`, with a `PiholeSource` first backend reading the FTL `/api/queries` endpoint. A small detector counts per-(MAC, domain) resolutions and flags a sustained loop; a flagged MAC is fed to the existing `Actor.handle`.

**Pros:** Pi-hole already logs **every** query with domain + client + timestamp — exactly the raw stream rate detection needs; cheap to poll once per scan cycle; the network already runs Pi-hole. The `DnsSource` Protocol mirrors `Controller`, so a future AdGuard/Unbound backend slots in without touching the detector.
**Cons:** a new optional dependency on an external service; clients with hardcoded upstream DNS bypass Pi-hole and are invisible (see Decision); a new data-source lifecycle to own.

### Option 3: A packet-capture / passive DNS sniffer on the daemon host

Sniff DNS on the wire.

**Pros:** sees every client regardless of resolver (no hardcoded-DNS blind spot).
**Cons:** needs the daemon on a span/mirror port or inline, raw-socket privileges, and per-vLAN visibility this home-lab topology doesn't give it. Far more operational surface for a signal Pi-hole already serves. Rejected.

## Decision

**Chosen Option:** Option 2 — an optional `DnsSource` backend (Pi-hole v6 FTL first), feeding a `DnsThrashDetector` whose flags route through the existing `Actor.handle`.

**Rationale:** the raw query stream is the only thing that can tell 478-resolves from 2, and Pi-hole already has it. Modeling it as a Protocol alongside `Controller` keeps the detector vendor-agnostic and reuses the entire remediation pipeline for free, satisfying "additive, not a rewrite".

**Forks resolved:**

- **`DnsSource` Protocol, mirroring `Controller`.** `dns_sources/base.py` declares a frozen `DnsQuery(ts, client_ip, domain)` and a `runtime_checkable` `DnsSource` Protocol (`login` / `queries_since` / `close`). `PiholeSource` owns its aiohttp session lazily (created in `login()`, torn down in `close()`) exactly like `UniFiController`. Auth is `POST /api/auth {"password": …}` → `session.sid`; queries are `GET /api/queries?from=<unix>&length=10000` with the sid sent as the `X-FTL-SID` header (Pi-hole v6 also accepts a `sid` query param or cookie — the header keeps the secret out of the request line). The default page size is 100, far below a busy resolver's per-poll volume, so a generous `length` is requested to avoid silently truncating the very thrash being counted. On a 401 (expired sid) the source **re-authenticates once and retries once**.
- **Exact-FQDN matching in v1; eTLD+1 grouping deferred.** The detector counts by the **exact** queried name. Grouping by registrable domain (eTLD+1) would catch a device that thrashes across `a.example.com` / `b.example.com`, but doing it correctly needs a public-suffix list (you cannot split `foo.co.uk` by counting dots), i.e. a new dependency and its update cadence. The incident — and the common case — is a single hardcoded broker hostname, which exact-FQDN catches. eTLD+1 is documented as a future enhancement, intentionally not built.
- **Multi-instance merging, tolerant of a dead instance.** A `MergedDnsSource` composite holds one `PiholeSource` per configured instance (all sharing the spec's password) and concatenates their `queries_since` results. A per-instance failure logs a warning and contributes nothing — **one dead Pi-hole must never blind the other**. `login()` is likewise tolerant so one unreachable instance at startup doesn't abort the daemon.
- **Hardcoded-DNS clients are a documented blind spot.** A client configured with an upstream resolver (8.8.8.8, etc.) bypasses Pi-hole and never appears in `/api/queries`. This is **acceptable and not solved** — it is the nature of a resolver-log data source. Documented here and in `config.example.yaml`; a client that must be watched should be pointed at Pi-hole.
- **Fail-soft runtime, fail-closed config.** A source fetch failure in the detector logs `dns_source_unavailable` and returns **no flags for that cycle** — the additive DNS signal never breaks the scan loop or the RF path (Req 4). Conversely, config validation is fail-closed: an unknown source `type`, a missing/empty `password`, a bad instance `url`, or a `detection.dns_thrash` block **with no `dns_sources` configured** (a detection the operator believes is armed but that has no data) all raise a clear `ValueError` at load.
- **Additive routing through the existing actor.** The detector returns flagged MACs; the scanner skips any that are allowlisted (canonical compare, like the scorer), looks up the MAC's `ClientSnapshot` for this cycle, and calls `Actor.handle(client, {"trigger": "dns_thrash"})`. The decision dict is logged verbatim in the `would_kick` line so the trigger is identifiable; **`kick_events` schema is unchanged**. Dry-run, backoff, caps, quiet-hours, rate-limits, and HA notification all apply by virtue of going through the one actor.
- **MAC↔IP join via a new fail-soft `ClientSnapshot.ip`.** The UniFi backend fills it from `raw.get("ip")` (non-empty `str` else `None`) — **fail-soft**, never `_require`'d, since a missing IP just means that client is invisible to the optional DNS signal (it must not fail-close the RF detection path). Queries from an IP with no matching client, and clients with no known IP, are simply skipped.
- **SIGHUP retunes thresholds in place; source wiring needs a restart.** `Scanner.update_config` retunes the detector's `dns_thrash` thresholds in place (the deques are time-pruned, no rebuild needed), mirroring the ADR-0004/0007 reload posture. Changing a source **URL or password**, or turning the feature on/off, requires a restart — the same Phase-1 limitation as the HA notifier and reboot scheduler, which are also built once at startup.

## Acceptance Criteria

- [x] **AC-1**: Config parses the `dns_sources:` list and the `detection.dns_thrash:` block to typed, frozen config; malformed entries (unknown `type`, missing `password`, bad/missing instance `url`, empty `instances`, non-positive `window_minutes`/`sustain_windows`, negative/bool `same_domain_queries_max`) fail closed with clear errors; absent blocks leave the feature off (`detection.dns_thrash is None`, `dns_sources == ()`) with zero behavior change; a `dns_thrash` block with no `dns_sources` is a config error.
- [x] **AC-2**: The Pi-hole password never appears in a config-error message (mirrors `test_config_secret_redaction.py`; `password` stays a `_SECRET_KEYS` entry, and `DnsSourceSpec.password` is `repr=False`).
- [x] **AC-3**: `PiholeSource` authenticates (`POST /api/auth` → `session.sid`), fetches queries with `from=` and the sid header, parses rows (`time` / `client.ip` / `domain`) into `DnsQuery`, and on an expired sid (401) re-authenticates **exactly once** then retries and succeeds.
- [x] **AC-4**: `MergedDnsSource` combines queries from two instances; a down instance degrades gracefully — it logs a warning and contributes nothing while the other instance's data is still used (`login` is likewise tolerant).
- [x] **AC-5**: A MAC resolving one domain more than the threshold within the window, sustained continuously for `sustain_windows * window_minutes`, is flagged; under-threshold, not-yet-sustained, or spread-across-different-domains traffic is not; dropping under threshold resets the sustain streak.
- [x] **AC-6**: A flagged MAC routes through `Actor.handle` — with `dry_run: true` the `would_kick` path fires (trigger tagged `dns_thrash`) and no controller call is made; with `dry_run: false` the real deauth + `kick_event` + notify fire, proving the backoff/rate-limit/notify machinery is exercised, not bypassed.
- [x] **AC-7**: Allowlisted MACs are never flagged/kicked by this path (a spared allowlisted MAC beside a kicked non-allowlisted one proves the filter).
- [x] **AC-8**: The MAC↔IP join comes from `ClientSnapshot.ip` (fail-soft); clients with an unknown IP are skipped without error; queries from IPs with no matching client are ignored.
- [x] **AC-9**: The per-MAC `dns_same_domain_queries_max` override resolves override > global (consistent with `resolve_caps`).
- [x] **AC-10**: A source fetch failure logs a warning (`dns_source_unavailable`) and the scan cycle completes normally (no crash, no flag); a SIGHUP-style `update_config` picks up changed `dns_thrash` thresholds in place and `Scanner.update_config` propagates them to the detector.

## Consequences

### Positive

- Catches an entire failure class (application-layer reconnect loops) the RF scorer is blind to, with a high-signal / low-false-positive indicator, using data Pi-hole already collects.
- Strictly additive and opt-in: omit the config → today's behavior; a DNS flag reuses the whole `Actor.handle` policy stack (dry-run, backoff, caps, quiet-hours, rate-limits, notify) with no new remediation code and no `kick_events` schema change.
- `DnsSource` is a Protocol mirroring `Controller`, so a second resolver backend (AdGuard, Unbound, passive sniffer) is a new class plus a `type:` entry, not a detector rewrite.

### Negative

- A new optional runtime dependency on an external service (Pi-hole). Mitigated by fail-soft: a down source yields no flags and never breaks the scan loop.
- **Hardcoded-DNS clients are invisible** to this signal — a real, unfixable limitation of a resolver-log source. Documented; point a watched client at Pi-hole.
- Exact-FQDN matching misses a device that thrashes across many sibling hostnames. Deferred (eTLD+1 needs a public-suffix dependency); the single-broker-hostname case is covered.
- Source URL/password changes and on/off toggles need a restart (SIGHUP retunes only thresholds) — consistent with the HA-notifier / reboot-scheduler Phase-1 limitation.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| A down Pi-hole silently disables detection or crashes the loop | Medium | Medium | Fail-soft: warn + no flags; RF path untouched; multi-instance merge tolerates one dead instance |
| Over-eager flagging kicks a healthy but chatty device | Low | Low | Sustain-window requirement; `dry_run` first; ADR-0007 caps/quarantine; allowlist; per-MAC override |
| `${PIHOLE_PASSWORD}` leaks into a startup/SIGHUP error line | Low | High | `password` in `_SECRET_KEYS` (redacted at any depth); `repr=False` on the spec; AC-2 |
| Pi-hole default `length=100` truncates a busy resolver's poll | Medium | Medium | Request a generous `length`; poll the delta since the last cycle |
| A device thrashes across sibling hostnames, evading exact-FQDN | Low | Low | Documented; eTLD+1 grouping is a scoped future enhancement |

## Implementation Plan

- [x] **Data source** — `dns_sources/` package: `base.py` (`DnsQuery` + `DnsSource` Protocol), `pihole.py` (`PiholeSource` v6 FTL auth/fetch/parse/re-auth + `MergedDnsSource`), `__init__.py` factory `build_dns_sources` mirroring `controllers/__init__.py`.
- [x] **Snapshot** — `ClientSnapshot.ip: str | None` (fail-soft); UniFi backend fills it from `raw.get("ip")`.
- [x] **Config** — `DnsThrashConfig`, `DnsSourceSpec`/`DnsInstanceSpec`, `DetectionConfig.dns_thrash`, `Config.dns_sources`, `OverrideEntry.dns_same_domain_queries_max`; fail-closed builders; `dns_thrash`-without-`dns_sources` rejection.
- [x] **Detector** — `dns_thrash.DnsThrashDetector`: per-(MAC, domain) timestamp deques, sustain-streak markers, injected clock; fail-soft on source error; `update_config` retune.
- [x] **Resolution** — `resolve_dns_same_domain_max` (override > global, like `resolve_caps`).
- [x] **Wiring** — composition root (`main.Daemon`) builds the source + per-scanner detector and owns the source `login`/`close` lifecycle; `Scanner.run_once` runs detection after the per-client loop and routes flags through `Actor.handle`; SIGHUP propagates threshold changes.
- [x] **Tests** — `tests/test_dns_config_ac1.py`, `test_dns_secret_redaction_ac2.py`, `test_dns_sources_ac3.py`, `test_dns_sources_ac4.py`, `test_dns_thrash_ac5.py`…`ac10.py` for AC-1…AC-10; UniFi `ip` mapping test.
- [x] **Docs** — this ADR + index row; `config.example.yaml` documented (commented-out) blocks; `env.example` gains `PIHOLE_PASSWORD`.

## Related ADRs

- [ADR-0007](./0007-action-policy-backoff-and-quiet-hours.md) — the backoff/caps/quiet-hours policy a DNS flag inherits by routing through `Actor.handle`.
- [ADR-0004](./0004-kick-rate-limits.md) — the rate limits a DNS flag also inherits; the in-place SIGHUP-reload posture this mirrors.
- [ADR-0009](./0009-disable-able-detection-criteria.md) — the RF client criteria this complements with an orthogonal application-layer signal.
- [ADR-0001](./0001-mvp-scope-base-feature.md) — §3 detection scope, the `Controller` Protocol shape `DnsSource` mirrors, and `override > global` resolution.

## References

- Pi-hole v6 FTL REST API — `POST /api/auth`, `GET /api/queries` (FTL OpenAPI spec).
- GitHub issue #18 — the 2026-07-15 Meross incident and the data-source problem.
- `src/wifi_shepard/dns_sources/`, `src/wifi_shepard/dns_thrash.py`, `src/wifi_shepard/config.py`, `src/wifi_shepard/scanner.py`.
