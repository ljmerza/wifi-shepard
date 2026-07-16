# ADR-0012: DNS/Pi-hole Observability Persistence & UI Surface

**Status:** Accepted
**Date:** 2026-07-16
**Author:** Leonardo Merza

## Context

### Background

ADR-0011 shipped DNS-thrash detection and it is now live (two Pi-hole v6 instances, `gym` + `bonus`, enabled 2026-07-16). But an operator has **no way to see it working**: Pi-hole login success is silent, the detector's per-(MAC, domain) counts live only in memory, and a DNS-thrash kick is indistinguishable in the database from an RF kick. The read-only UI sidecar (ADR-0002) shows nothing about DNS because there is nothing persisted to show.

The concrete ask: a `/dns` page that proves the integration is alive — both resolvers authenticating and polling, who is approaching the thrash threshold, and which kicks DNS actually triggered — even during the (common) periods when nothing is thrashing.

### Current State

- **DB schema** (`db.py`) has `client_samples`, `kick_events`, `reboot_events`, `ap_samples`, `ap_radio_samples`. There is **no DNS table**, and `kick_events` has no reason/trigger column — `mechanism` records the *method* (deauth/btm/deauth_fallback), not *why*.
- **`Actor.handle(client, ctx)`** receives a context dict that already distinguishes triggers — dns_thrash passes `{"trigger": "dns_thrash"}` (scanner.py:131), inactivity passes a dict carrying `trigger=inactivity` (scanner.py:99), the scorer passes its `decision` (scanner.py:93, no explicit trigger). The actor uses this only for the `would_kick` log line and HA notification; it **never persists it** (`kick_events` schema unchanged, per ADR-0011's decision).
- **`DnsThrashDetector`** holds exactly the data a "near-threshold" view needs — `_counts[mac][domain] → deque[query ts]` (time-pruned to the window) and `_over_since[mac]` (streak start) — but exposes only the flagged-MAC list from `observe()`.
- **`MergedDnsSource.queries_since`** concatenates both instances and swallows a per-instance failure with a `dns_source_login_failed`/warning log; it does not expose per-instance outcomes to a caller.
- **UI sidecar** is a strictly read-only FastAPI app over the daemon's SQLite (GET-only, AC-6), already tolerant of absent tables (the AP tables degrade to an empty state rather than 500).

### Requirements

1. **Prove liveness without a thrash event** — show that each Pi-hole instance authenticated and is polling, with recency and query volume, even when zero MACs are flagged.
2. **Show who's close** — surface per-(MAC, domain) standings approaching the threshold, so an operator can see the signal building before a kick.
3. **Attribute kicks** — make a DNS-thrash-triggered kick distinguishable from an RF or inactivity kick in the DB and UI.
4. **Read-side only in the UI** — the sidecar must stay GET-only and must not gain Pi-hole credentials or network reach; it reads what the daemon persists (ADR-0002 boundary).
5. **Additive & opt-in** — DNS off (no `dns_sources`) → no new rows, no behavior change, existing suite green.
6. **Partial-deploy safe** — a new UI image against an old daemon DB (missing the tables/column) must render, not 500 — the same posture the AP tables already have.

### Constraints

One process, asyncio. Match `db.py`'s forward-compatible `ALTER TABLE`-per-column migration style (ADR-0003/0010 precedent) and the fail-soft rule from ADR-0011 — **observability persistence must never break the scan loop**. The UI must preserve the ADR-0002 read-only fence and every pinned-markup contract in the existing test suite.

## Options Considered

### Option 1: Tag kicks with a trigger only

Add a `trigger` column to `kick_events` and show it in device history; do not persist any DNS-source or near-threshold data.

**Pros:**
- Smallest schema touch (one column + migration).
- Immediately attributes existing/future kicks.

**Cons:**
- Shows **nothing** until a kick actually fires — cannot prove Pi-hole is polling during the common quiet periods, which is the operator's actual question.
- No "signal building" visibility.

### Option 2: Health heartbeat + status card

Persist one `dns_source_samples` row per instance per poll (ts, source, ok, query_count, error); UI shows a source-health card + volume sparkline.

**Pros:**
- Directly proves liveness even with zero thrash.
- Minimal schema (one table).

**Cons:**
- No visibility into *who* is thrashing or approaching the limit.
- Doesn't attribute kicks.

### Option 3: Full DNS observability page (Chosen)

All of Option 2, **plus** a `dns_thrash_observations` table (near-threshold snapshot from the detector) **plus** the `kick_events.trigger` column, surfaced on a dedicated `/dns` page and an overview status card.

**Pros:**
- Answers all three of the operator's questions — is it polling, who's close, what did it kick — in one surface.
- Reuses the detector's existing in-memory standings; no new counting logic.

**Cons:**
- Largest change here: two new tables + one column + three migrations, plus a new UI route and its tests.
- Two observability tables grow over time (bounded per-poll; see Risks).

## Decision

**Chosen Option:** Option 3 — persist per-poll source health, near-threshold observations, and a kick trigger; surface them on a read-only `/dns` page + an overview card.

**Rationale:** the operator's question is "is it working," which Option 1 can't answer (nothing until a kick) and Option 2 answers only partially (no signal-building, no attribution). Option 3 reuses data the daemon already computes — the detector's live `_counts`/`_over_since`, the merged source's per-instance poll outcomes, and the trigger context the actor already receives — so the cost is persistence + rendering, not new detection logic.

**Forks resolved:**

- **Three persistence targets, all fail-soft.** Writing observability rows happens *after* detection each cycle and is wrapped so a DB error logs and is swallowed — it must never break the scan loop or the RF path (ADR-0011 Req 4). A write failure degrades the UI's freshness, nothing more.
- **Per-instance health needs source-level instrumentation.** `MergedDnsSource` records the outcome of its most recent `queries_since` per child instance — `(name, ok, query_count, error)` — as a readable snapshot (`last_poll_status()`), since the detector calls the *merged* source and per-instance results are otherwise lost. The scanner reads this after `observe()` and writes one `dns_source_samples` row per instance. A down instance writes `ok=0` + error and does not suppress the healthy instance's row (mirrors the merge's existing tolerance).
- **Near-threshold snapshot from the detector's live state.** `DnsThrashDetector` records, during its existing `observe()` pass (no second iteration), the current standings per (MAC, domain): `count`, resolved `threshold`, and `over_since`. It exposes them via a `standings()` snapshot. The scanner persists only **contenders** — rows with `count ≥ ceil(near_ratio × threshold)` (default `near_ratio = 0.5`), capped at a top-N per poll — so quiet domains don't flood the table. Rows carry the same wall-clock `ts` as the poll.
- **`kick_events.trigger`, derived from the handle context.** A new `trigger TEXT NOT NULL DEFAULT 'rf'` column. `Actor.handle` reads `ctx.get("trigger", "rf")` and passes it to `insert_kick`, so dns_thrash → `'dns_thrash'`, inactivity → `'inactivity'`, scorer → `'rf'`. The forward-compatible migration backfills **pre-existing rows to `'rf'`** — the historical kicks predate DNS/inactivity enablement and were all RF deauths, so this is accurate for the live DB (documented assumption, not a guess about unknowable rows).
- **`/dns` is a read-only projection, partial-deploy safe.** A new `GET /dns` route on the sidecar reads the two tables + `kick_events.trigger` through the same `_safe_read` path the other pages use. Absent tables/column (old daemon DB) degrade to an empty "no DNS data yet" state, exactly like the AP tables — verified by a back-compat test. The route adds no write verbs, so the ADR-0002 AC-6 fence still passes. An overview status card gives an at-a-glance "gym ✓ / bonus ✓" summary linking to `/dns`.
- **Retention: bounded per-poll, pruned by age.** `dns_source_samples` grows at ~(instances × polls/day) ≈ 2 880 rows/day; `dns_thrash_observations` at ≤ top-N/poll. Both are pruned to a rolling **7-day** window on write (a cheap `DELETE WHERE ts < now-7d`), keeping them observability-sized rather than unbounded like `client_samples`.

## Acceptance Criteria

- [ ] **AC-1**: A forward-compatible migration adds `kick_events.trigger` (`TEXT NOT NULL DEFAULT 'rf'`, existing rows backfilled to `'rf'`) and idempotently creates `dns_source_samples` and `dns_thrash_observations`; opening a pre-ADR-0012 DB upgrades it in place with no data loss and no error.
- [ ] **AC-2**: With DNS enabled, each scan cycle writes one `dns_source_samples` row per configured instance — a healthy poll records `ok=1` with the fetched `query_count`; a failing instance records `ok=0` with its error **and** the other instance's healthy row is still written the same cycle.
- [ ] **AC-3**: After `observe()`, the detector's `standings()` reflects live per-(MAC, domain) `count`/`threshold`/`over_since`; the scanner persists only contenders (`count ≥ ceil(0.5 × threshold)`, top-N capped) to `dns_thrash_observations`, and a below-band domain produces no row.
- [ ] **AC-4**: A kick routed through `Actor.handle` records `kick_events.trigger` from the context — `'dns_thrash'` for a DNS flag, `'inactivity'` for an ADR-0010 flag, `'rf'` for a scorer flag — so a DNS-triggered kick is distinguishable from an RF kick in the DB.
- [ ] **AC-5**: With no `dns_sources` configured, no `dns_source_samples` or `dns_thrash_observations` rows are written, `kick_events.trigger` defaults to `'rf'`, and the existing daemon test suite is unaffected (opt-in, zero behavior change).
- [ ] **AC-6**: Observability writes are fail-soft — a DB write error during persistence logs and is swallowed; the scan cycle completes, detection still flags, and the RF path is untouched.
- [ ] **AC-7**: `GET /dns` renders per-instance source health (name, ok/fail, last-poll age, query volume) from `dns_source_samples`, the near-threshold table (mac, name, domain, `count/threshold`, sustained duration) from `dns_thrash_observations`, and recent `trigger='dns_thrash'` kicks; each section shows an empty state when its data is absent.
- [ ] **AC-8**: The sidecar stays read-only (the AC-6 no-write-route fence still passes with `/dns` added) and `GET /dns` (plus the overview card) renders HTTP 200 against a DB missing the new tables/column, showing the empty state instead of a 500.

## Consequences

### Positive

- The operator can confirm at a glance that both Pi-holes are authenticating and polling, watch a thrash signal build before it kicks, and see exactly which kicks DNS caused — closing the ADR-0011 observability gap.
- Reuses data already computed (detector standings, merged-source outcomes, actor trigger context); no new detection or counting logic.
- Kick attribution (`trigger`) benefits every detection class, not just DNS — RF vs inactivity vs dns_thrash become distinguishable in history.

### Negative

- Two new observability tables and a per-poll write path; the daemon now writes on every cycle even when nothing is flagged (bounded, pruned to 7 days).
- Per-instance health requires `MergedDnsSource` to expose last-poll outcomes — a small widening of that class's surface.
- A new UI route and templates to maintain, plus back-compat handling for the partial-deploy window.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Observability write error breaks the scan loop | Low | High | Fail-soft: writes wrapped, logged, swallowed (AC-6); RF path untouched |
| Near-threshold table floods with quiet domains | Medium | Low | Persist only contenders (`≥ 0.5 × threshold`), top-N capped, 7-day prune (AC-3) |
| Tables grow unbounded like `client_samples` | Low | Medium | Rolling 7-day prune on write; low per-poll row count |
| `trigger` backfill mislabels historical kicks | Low | Low | Live DB's historical kicks are all RF deauths (verified); backfill to `'rf'` is accurate, documented |
| New `/dns` route regresses the read-only guarantee | Low | High | GET-only; AC-6 fence re-run; no Pi-hole creds in the sidecar (AC-8) |
| Old daemon DB + new UI 500s on `/dns` | Medium | Medium | `_safe_read` empty-state path + explicit back-compat test (AC-8) |

## Implementation Plan

- [ ] **Schema/migrations** (`db.py`) — `SCHEMA_DNS_SOURCE_SAMPLES`, `SCHEMA_DNS_THRASH_OBSERVATIONS`; `kick_events.trigger` via `_KICK_EVENTS_MIGRATIONS`; `insert_dns_source_sample`, `insert_dns_thrash_observations`, 7-day prune; `insert_kick` gains `trigger`.
- [ ] **Source health** (`dns_sources/pihole.py`) — `MergedDnsSource` records per-child `queries_since` outcome; `last_poll_status()` accessor.
- [ ] **Detector standings** (`dns_thrash.py`) — record per-(MAC, domain) `count`/`threshold`/`over_since` during `observe()`; `standings()` snapshot.
- [ ] **Actor** (`actor.py`) — read `ctx.get("trigger", "rf")`, thread to `insert_kick`.
- [ ] **Scanner wiring** (`scanner.py`) — after `_run_dns_thrash`, persist source health + contender standings (fail-soft).
- [ ] **UI read model** (`wifi_shepard_ui/views.py`) — `dns_source_health()`, `dns_near_threshold()`, `dns_thrash_kicks()`; absent-table tolerance.
- [ ] **UI route + templates** — `GET /dns` + `dns.html`; overview status card; nav entry; `active_page`.
- [ ] **Tests** — daemon AC-1…AC-6; UI AC-7…AC-8 (incl. read-only fence + partial-deploy back-compat).
- [ ] **Docs** — this ADR + index row; `config.example.yaml` note that `/dns` visualizes the feature.

## Related ADRs

- [ADR-0011](./0011-dns-thrash-detection-pihole.md) — the DNS-thrash detector and Pi-hole source this makes observable; the fail-soft rule inherited here.
- [ADR-0002](./0002-device-history-and-status-ui.md) — the read-only UI sidecar the `/dns` page extends; the GET-only fence and partial-deploy posture reused.
- [ADR-0010](./0010-traffic-inactivity-detection.md) — the `inactivity` trigger the new `kick_events.trigger` column also attributes.
- [ADR-0001](./0001-mvp-scope-base-feature.md) — the persistence-schema domain this extends; `override > global` threshold resolution feeding `standings()`.

## References

- `src/wifi_shepard/db.py`, `dns_thrash.py`, `dns_sources/pihole.py`, `actor.py`, `scanner.py`, `wifi_shepard_ui/`.
- ADR-0011 live enablement 2026-07-16 (gym `192.168.1.186`, bonus `192.168.1.189`).
