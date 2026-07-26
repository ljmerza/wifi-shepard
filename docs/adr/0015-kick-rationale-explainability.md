# ADR-0015: Kick Rationale — Persist and Surface Why Each Kick Fired

**Status:** Accepted
**Date:** 2026-07-23
**Author:** Leonardo Merza

## Context

### Background

The daemon decides to kick a client from a sliding window of samples measured against per-MAC-resolved thresholds, optionally tightened by quiet hours (ADR-0007), gated on AP saturation (ADR-0008), with individual criteria disable-able (ADR-0009) — and three independent detectors can raise the flag (RF scoring, traffic inactivity per ADR-0010, DNS thrash per ADR-0011). That is a lot of moving policy.

None of it is recorded. A kick lands in `kick_events` as `(ts, mac, dry_run, mechanism, target_bssid, attempt_group, trigger)`. `mechanism` is *how* (btm / deauth / deauth_fallback) and `trigger` is *which detector* (ADR-0012), but nothing says **why** — which values breached, which limits were in force, whether quiet hours or a per-MAC override changed them. The device timeline on `/devices/{mac}` can only render the badge "kick".

The operator question this closes: *"why did wifi-shepard kick my WLED at 02:14 last Tuesday?"* — answerable today only by guessing from `client_samples` and hoping the config hasn't changed since.

### Current State

- **`Actor.handle`** (`actor.py:69`) builds a `reason` dict — `signal`, `tx_rate_kbps`, `tx_retries`, `wifi_tx_attempts`, `radio` — and **logs it only on the dry-run path** (`actor.py:86`). A kick that actually fires logs nothing at all and persists nothing beyond the four existing columns.
- **The dry-run path never writes a `kick_events` row.** It logs `would_kick` and returns. Consequently the UI's `dry_run_count` tile (`views.py:410`) is structurally always `0`, and a dry-run soak leaves no queryable record of what the daemon would have done.
- **`Scorer.ingest`** resolves thresholds, applies quiet-hours tightening, and returns the resolved `thresholds` dict as its decision. The actor receives the *post-tightening* values but has no way to tell that tightening occurred, or that a per-MAC `overrides:` entry contributed.
- **`is_bad_state`** requires *every* sample in the window to breach *every* active criterion. So the most recent sample necessarily breaches all of them — the actor already holds a fully representative witness.
- **Non-RF decision dicts are uneven.** Inactivity passes real evidence (`window_bytes`, `min_bytes_per_window`, `window_samples` — `inactivity.py:111`); DNS thrash passes only `{"trigger": "dns_thrash"}` (`scanner.py:151`) even though `DnsThrashDetector.standings()` holds the domain, count, and resolved threshold.
- **UI sidecar** is read-only over the same DB, already tolerant of missing columns (`name` degrades via `try/except sqlite3.OperationalError`) and guarded by `_REQUIRED_KICK_EVENTS_COLUMNS` for the columns it hard-depends on.
- **Two persistence backends** — SQLite (`db.py`) and MySQL/MariaDB (`db_mysql.py`, live per deployment) — both implement the `Store` Protocol and both must migrate.

### Requirements

1. **Explain a specific kick, after the fact** — for any row in the ledger, show the values observed, the limits in force, and which criteria breached.
2. **Truthful under config change** — the explanation must reflect the policy at *decision* time, not today's config.
3. **Cover every trigger** — RF, inactivity, and DNS thrash, without a schema shape per detector.
4. **Explain would-kicks too** — a dry-run soak is exactly when "why would you do that?" matters most; make the existing `dry_run` column and tile real.
5. **Log symmetry** — a live kick should be at least as self-explaining in the logs as a dry-run one is today.
6. **Partial-deploy safe** — new UI against an old daemon DB must render, not 500; new daemon against an old UI must not break it.
7. **Additive** — no change to detection, backoff, caps, rate limits, or any existing count/state derivation.

### Constraints

One process, asyncio. Match `db.py`'s forward-compatible `ALTER TABLE`-per-column migration style (ADR-0003/0010/0012 precedent) and mirror it in `db_mysql.py`. Preserve the ADR-0002 read-only fence on the sidecar and every pinned-markup contract in the existing suite. `kick_events` is the backoff ledger (`recent_kick_timestamps`) — anything written to it must not perturb ADR-0007 caps.

## Options Considered

### Option 1: Rationale JSON blob on `kick_events` (Chosen)

Add a nullable `rationale TEXT` column holding a JSON snapshot the actor builds at decision time: trigger, observed values, thresholds in force, breached criteria, quiet-hours and override flags, window size.

**Pros:**
- One row tells the whole story — no join, no re-derivation, no dependency on `client_samples` retention.
- Frozen at decision time, so it stays truthful after a config edit (Req 2).
- Schema-flexible: each detector fills its own evidence keys under a shared envelope; future detectors need no migration (Req 3).
- Same one-column `ALTER TABLE` pattern already used four times on this table.
- Identical on SQLite and MySQL.

**Cons:**
- Opaque to SQL aggregation ("which criterion fires most?") without JSON functions.
- Duplicates values also present in `client_samples`.
- The blob shape becomes an implicit contract between daemon and UI.

### Option 2: Normalized `kick_reasons` sidecar table

A typed table keyed to the kick row, one column per signal (observed + limit pairs) plus quiet-hours / AP-CU / window columns.

**Pros:**
- Fully queryable and indexable; cheap aggregate charts.
- No JSON parsing in the read model.

**Cons:**
- Rigid: every new detector needs new columns or a wide row of NULLs — DNS thrash would fill 2 of 11.
- Doubles the migration surface across both backends, and adds a join to every history read.
- `insert_kick` becomes a two-statement write needing `lastrowid` plumbing the `Store` Protocol does not expose.

### Option 3: Structured logs only

Emit the existing `reason` dict on the live path too, with thresholds and quiet-hours context. Nothing persisted.

**Pros:**
- Tiny diff, no schema change, no storage growth, zero read-path risk.
- Closes the real asymmetry — dry-run explains itself, a live kick says nothing.

**Cons:**
- Cannot answer "why was this kicked three days ago" from the UI, which is the actual ask.
- Docker log rotation loses the record; no per-device surface.

### Option 4: Reconstruct rationale at read time from `client_samples`

The UI queries the N samples preceding each kick's `ts` and re-applies thresholds.

**Pros:**
- Zero write-path change; retroactive for kicks already in the ledger; can plot the real sample trend.

**Cons:**
- **Lies after a config edit** — re-applies *today's* thresholds to a kick made under yesterday's, violating Req 2. Per-MAC overrides and quiet-hours tightening are unrecoverable.
- Breaks past the 30-day `client_samples` prune — old kicks silently lose their why.
- Cannot explain inactivity or DNS-thrash kicks at all; `client_samples` has no matching columns.

## Decision

**Chosen Option:** Option 1 — a nullable `rationale` JSON column on `kick_events`, written by the actor at decision time for both live and dry-run kicks, surfaced as a per-kick "Why" on the device timeline.

**Rationale:** Req 2 (truthful under config change) eliminates Option 4 outright and Req 1 eliminates Option 3. Between the two persisting options, Req 3 decides it: three detectors with disjoint evidence shapes, and more coming, fit a versioned envelope far better than a wide sparse table — and Option 2's `lastrowid` plumbing would widen the `Store` Protocol that ADR-0001 deliberately kept narrow. Cross-row analytics is not a stated requirement; if it becomes one, the blob can be projected into a table later without re-deciding the write path.

**Forks resolved:**

- **Envelope shape, versioned.** One JSON object per kick:

  ```json
  {"v": 1, "trigger": "rf", "window_samples": 5,
   "quiet_hours": false, "override": false,
   "observed": {"signal": -78, "tx_rate_kbps": 6000, "retry_pct": 41.2,
                "radio": "ng", "ap_cu_total": 74},
   "thresholds": {"signal_dbm_max": -70, "tx_rate_kbps_max": 12000,
                  "retry_pct_max": 30, "ap_cu_total_min": 60},
   "breached": ["signal", "tx_rate_kbps", "retry_pct"]}
  ```

  `v` is the contract handle: the UI renders what it understands and degrades on the rest, so a future daemon can add keys without breaking an older sidecar. `observed`/`thresholds`/`breached` are RF-specific; other triggers carry their own keys under the same `v`/`trigger`/`window_samples` envelope.

- **`breached` is derived, not re-scored.** `is_bad_state` demands every sample breach every *active* criterion, so the witness sample the actor already holds breaches exactly the set of criteria whose threshold is non-`None`. The actor computes `breached` from that sample against the resolved thresholds — no scorer change, and a disabled criterion (ADR-0009 `null`) appears in neither `thresholds` nor `breached`, so the rationale never implies a signal was tested when it was not.

- **Quiet hours is flagged by the scorer, not re-derived by the actor.** `Scorer.ingest` already knows whether it tightened; it stamps `quiet_hours: true` into the decision dict it returns. Re-evaluating `quiet_hours_active` inside the actor would disagree at a window boundary and would wrongly claim tightening on the inactivity/DNS paths, which never apply it. `thresholds` records the **tightened** values — the limits actually applied.

- **`override` is a boolean, matched exactly as `resolve_thresholds` matches.** Whether an `overrides:` entry applied to this MAC, compared with the same equality `resolution.py` uses, so the flag cannot claim an override that did not actually contribute. Which *fields* it changed is deliberately out of scope — the flag plus the recorded effective limits already answer "was this device on custom policy?".

- **DNS thrash gains real evidence; inactivity already has it.** The scanner reads `detector.standings()` for each flagged MAC and passes the top offending `(domain, count, threshold)` into the decision dict, so a DNS kick explains itself like an RF one. Inactivity's existing dict (`window_bytes`, `min_bytes_per_window`, `window_samples`) is carried through unchanged.

- **Dry-run rows are written, throttled to the first backoff cooldown.** The dry-run branch inserts a `dry_run=1` row with the same rationale. Unthrottled this would write one row per bad-state client per scan cycle — a permanently-bad client floods the table, because the dry-run path returns before backoff. Throttling to `backoff.cooldowns_seconds[0]` (falling back to 300 s when the schedule is empty) gives the would-kick ledger roughly the same density as a live one, which is exactly the question a soak is asking. State is a per-MAC in-memory timestamp on the actor, deliberately not persisted: a restart re-arming one extra row per MAC is harmless.

- **Dry-run rows are provably inert.** `recent_kick_timestamps` (backoff/caps), `list_devices`, `overview`, and `device_summary.kick_count` all already filter `dry_run = 0`. The only reader that changes behavior is `device_summary.dry_run_count`, which becomes non-zero — that is the intended fix, not a regression.

- **Log symmetry.** The live path emits `logger.info("kick", extra={... rationale ...})` and the dry-run path's existing `would_kick` line carries the same payload, so `dca logs` and the UI tell the same story.

- **Partial-deploy posture, both directions.** `rationale` is **not** added to `_REQUIRED_KICK_EVENTS_COLUMNS` — doing so would hard-fail a new sidecar against an old daemon DB. The read model selects it inside the same `try/except OperationalError` degradation the `name` column uses, and a `NULL` or malformed value renders a dash. An old sidecar against a new daemon simply ignores the column.

## Acceptance Criteria

- [ ] **AC-1**: A forward-compatible migration adds `kick_events.rationale` (nullable `TEXT`) on both the SQLite and MySQL backends; opening a pre-ADR-0015 database upgrades it in place with existing rows keeping `NULL`, no data loss, and no error, and re-opening is idempotent.
- [ ] **AC-2**: A live RF kick writes a row whose `rationale` JSON records `v`, `trigger="rf"`, `window_samples`, the `quiet_hours` and `override` flags, the witness sample's `observed` values (signal, tx_rate_kbps, retry_pct, radio, ap_cu_total), the `thresholds` in force, and a `breached` list naming exactly the active criteria.
- [ ] **AC-3**: Given a criterion disabled with `null` (ADR-0009), the recorded `thresholds` carries no active limit for it and `breached` omits it — the rationale reflects only signals actually tested.
- [ ] **AC-4**: Given quiet hours active and tightening at least one threshold, the rationale records `quiet_hours: true` and the **tightened** limits; outside quiet hours it records `quiet_hours: false` and the untightened limits.
- [ ] **AC-5**: An inactivity kick records `window_bytes` / `min_bytes_per_window` / `window_samples`, and a DNS-thrash kick records the offending `domain`, `query_count`, and resolved `threshold`, both under the same envelope and with no new columns.
- [ ] **AC-6**: In `dry_run` mode the actor writes a `dry_run=1` row carrying the same rationale, throttled to at most one row per MAC per first-cooldown interval; a MAC flagged on consecutive cycles inside that interval produces exactly one row.
- [ ] **AC-7**: Dry-run rows are inert — with dry-run rows present, `recent_kick_timestamps`, `list_devices` state/counts, and `overview` totals are byte-identical to the same DB without them, while `device_summary.dry_run_count` reflects them.
- [ ] **AC-8**: Both paths log their rationale — a live kick emits a `kick` record and a dry-run emits `would_kick`, each carrying the same payload persisted to the row.
- [ ] **AC-9**: `GET /devices/{mac}` renders a one-line plain-English "why" per kick row plus an expandable observed-vs-threshold breakdown; a row with `NULL` or malformed rationale renders a dash.
- [ ] **AC-10**: Partial-deploy safe — the sidecar renders HTTP 200 against a DB with no `rationale` column (the column is absent from `_REQUIRED_KICK_EVENTS_COLUMNS`), and the ADR-0002 read-only route fence still passes.

## Consequences

### Positive

- Every kick, live or would-be, carries its own audit trail: what was measured, what the limit was, which policy layer set that limit. The `/devices/{mac}` timeline answers the operator's question directly.
- The explanation survives config edits and outlives the 30-day `client_samples` prune, because it is a snapshot rather than a reconstruction.
- The dry-run tile stops being a permanent zero, making a pre-flight soak genuinely reviewable before flipping `dry_run: false`.
- Live kicks become as observable in the logs as dry-run ones, closing an asymmetry that has existed since v1.
- DNS-thrash kicks gain the evidence the detector already computed but discarded.

### Negative

- A JSON blob is not SQL-queryable; cross-kick analytics ("top breach criterion this week") needs a later projection.
- The envelope becomes a daemon↔UI contract, versioned by `v` but still a coupling.
- `kick_events` now grows during dry-run soaks, where it previously did not grow at all.
- Rationale values duplicate what `client_samples` holds for the same instant.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Dry-run rows flood `kick_events` during a long soak | Medium | Medium | Per-MAC throttle at the first cooldown interval (AC-6); rows are one-per-MAC-per-5-min, not per-cycle |
| Dry-run rows perturb backoff, caps, or device state | Low | High | Every such reader already filters `dry_run = 0`; pinned by a before/after equivalence test (AC-7) |
| Rationale claims a threshold that was not actually tested | Medium | Medium | `breached` derived from the same non-`None` criteria `is_bad_state` tested; disabled criteria omitted (AC-3) |
| Quiet-hours flag disagrees with what the scorer applied | Medium | Low | Flag stamped by the scorer at tightening time, never re-derived downstream (AC-4) |
| New UI 500s against a pre-ADR-0015 DB | Medium | Medium | Column excluded from `_REQUIRED_KICK_EVENTS_COLUMNS`; `try/except` degradation + explicit back-compat test (AC-10) |
| Malformed/truncated JSON breaks the timeline | Low | Medium | Read model parses fail-soft to `None`; template renders a dash (AC-9) |
| Envelope drift between daemon and sidecar versions | Low | Low | `v` key; UI renders known keys and ignores unknown ones |

## Implementation Plan

- [ ] **Schema/migrations** — `rationale TEXT` in `SCHEMA_KICK_EVENTS` + `_KICK_EVENTS_MIGRATIONS` (`db.py`); mirror in `db_mysql.py`; `insert_kick(..., rationale: str | None = None)` on both and on the `Store` Protocol.
- [ ] **Scorer** (`scorer.py`) — stamp `quiet_hours: true` into the returned decision when `apply_quiet_hours` tightened.
- [ ] **Resolution** (`resolution.py`) — small helper reporting whether an `overrides:` entry matched a MAC, using the same equality as `resolve_thresholds`.
- [ ] **Rationale builder** (`actor.py`, module-level pure function) — assemble the envelope from `(client, ctx, thresholds, config)`; derive `breached`; JSON-serialize. Pure and unit-testable without a controller or DB.
- [ ] **Actor** — build once at the top of `handle`; write it on the fresh-kick, deauth-fallback, and dry-run inserts; add the per-MAC dry-run throttle; add the live `kick` log line and extend `would_kick`.
- [ ] **Scanner** (`scanner.py`) — pass the top `standings()` entry (domain / count / threshold) for each flagged MAC into the DNS-thrash decision dict.
- [ ] **UI read model** (`wifi_shepard_ui/views.py`) — `HistoryEvent.rationale`; select + `json.loads` fail-soft inside the existing absent-column tolerance; a pure `summarize_rationale()` producing the one-line sentence.
- [ ] **UI template** (`history.html`) — "Why" cell with the summary line and a `<details>` observed-vs-threshold breakdown; dash for absent rationale.
- [ ] **Tests** — daemon AC-1…AC-8; UI AC-9…AC-10 (including the read-only fence and the pre-column back-compat case).
- [ ] **Docs** — this ADR + index row; note the dry-run ledger behavior in `CLAUDE.md` / `config.example.yaml`.

## Related ADRs

- [ADR-0012](./0012-dns-observability-persistence.md) — added `kick_events.trigger` (*which detector*); this ADR adds *why that detector fired*, and gives DNS-thrash kicks the evidence `standings()` already holds.
- [ADR-0009](./0009-disable-able-detection-criteria.md) — `null` disables a criterion; the rationale must not imply a disabled signal was tested.
- [ADR-0007](./0007-action-policy-backoff-and-quiet-hours.md) — quiet-hours tightening and the cooldown schedule reused as the dry-run write throttle.
- [ADR-0008](./0008-ap-saturation-gate.md) — `ap_cu_total_min`, recorded as part of the limits in force.
- [ADR-0010](./0010-traffic-inactivity-detection.md) — the inactivity evidence carried through the envelope.
- [ADR-0003](./0003-kick-mechanism-upgrade.md) — `mechanism`/`attempt_group`, the *how* this complements; its BTM→deauth pair shares one rationale.
- [ADR-0002](./0002-device-history-and-status-ui.md) — the read-only sidecar and timeline this extends; the GET-only fence and partial-deploy posture reused.
- [ADR-0001](./0001-mvp-scope-base-feature.md) — the `kick_events` schema and `override > global` resolution this records.

## References

- `src/wifi_shepard/actor.py` (`reason` dict, dry-run early return), `scorer.py`, `resolution.py`, `scanner.py`, `db.py`, `db_mysql.py`.
- `src/wifi_shepard_ui/views.py` (`HistoryEvent`, `device_history`, `_REQUIRED_KICK_EVENTS_COLUMNS`), `templates/history.html`.
- `PLAN.md` §3–§4 (detection rules, backoff schedule).
