# ADR-0014: Per-Device Settings Editing — Device-Centric Write Surface over the Same config.yaml

**Status:** Accepted
**Date:** 2026-07-19
**Author:** Leonardo Merza

## Context

### Background

ADR-0013 made **every** config key editable from the sidecar, and it delivered: `settings_schema.py` carries 75 `FieldSpec`s covering the whole `Config` surface, and `tests/ui/test_settings_schema_coverage_ac1.py` fails if a new `Config` field is ever added without one. Nothing is un-editable.

What it did *not* deliver is a way to edit a setting **for the device you are looking at**. The Settings page is organized by *config section*, mirroring the YAML. The operator's mental model is organized by *device*: "this WLED is fine, stop touching it." Those two organizations collide for exactly the five per-MAC config paths, which live in four different Settings boxes:

| Per-device setting | YAML path | Settings box |
|---|---|---|
| Never touch this device | `allowlist` | Allowlist |
| Per-device threshold tuning (8 knobs) | `overrides[]` | Per-device overrides |
| Watch for a wedged session | `detection.inactivity.macs` | Detection |
| May be power-cycled | `reboot.eligible` | Reboot |
| How to power-cycle it | `reboot.overrides[]` | Reboot |

Today, allowlisting a device you just found on `/devices` means: read its MAC off the table, navigate to `/settings`, scroll to the Allowlist textarea, and re-type or paste the MAC into a newline-separated list — with no confirmation you typed the 17 characters correctly, and no indication on the device's own page afterward beyond a badge. The information needed to make the decision (kick count, state, signal history) is on one page; the control that acts on it is on another, keyed by a string you have to transcribe.

The ask: **set a device as allowlisted — and its other per-device settings — from the device itself.**

### Current State

- **The write pipeline exists and is sound** (ADR-0013). `POST /settings` runs `config_io.build_mapping` → `config_io.validate_mapping` (the daemon's own `build_config_from_mapping`) → `config_io.write_config` (ruamel round-trip, comments and `${VAR}` placeholders preserved, temp-file + `os.replace` atomic). The daemon file-watch (AC-6) picks the change up within one interval, no restart.
- **The read views already know the allowlist.** `app.py` calls `config_io.read_allowlist(config_path)` for both `/devices` and `/devices/{mac}`, and both templates already render an allowlisted badge (`devices.html` Allowlist column, `history.html` tile note). The data is on the page; only the control is missing.
- **The write fence is a literal-path allowlist.** `_assert_no_write_routes` (`app.py:131`) rejects any non-GET route whose path is not in `_ALLOWED_WRITE_PATHS`, which today holds exactly `{"/settings"}`. A new write route must be added there deliberately or the app refuses to start — ADR-0013's amendment of ADR-0002 was explicitly "a single-path allowlist, not a blanket lift."
- **`read_form_model` / `build_mapping` are an exact round-trip pair.** `read_form_model` returns `{scalars, scalar_lists, object_lists, section_enabled}`; `build_mapping` consumes precisely that shape. A per-device edit *could* be expressed as read-model → mutate → build → write with zero new mutation code.
- **…but that round-trip materializes defaults.** `_display_scalar` substitutes a field's schema default for any key absent from the file. Verified experimentally: round-tripping a 16-line config through `read_form_model` → `build_mapping` → `write_config` emits ~40 previously-absent keys (`backoff.*`, `safety_rails.*`, the whole `reboot.*` tree, `detection.inactivity.*`, `scanner.kick_mechanism`, `controllers: []`). The values are all the current *effective* values, so it is semantically a no-op — but a one-click device toggle producing a 40-line diff is not what "toggle allowlist" should mean.
- **A latent bug in the shipped Settings page.** `overrides[].name` — present in `config.example.yaml` as `name: "leonardo s22"` — has **no `FieldSpec`**, because `OverrideEntry` has no `name` field: `config.py:900` builds overrides with `{k: v for k, v in o.items() if k in known}`, silently dropping unknown keys. `config_io._build_item` likewise only emits leaves that have a `FieldSpec`, and `_overlay` replaces lists wholesale. Net effect, verified: **any Settings save silently deletes the `name:` label from every `overrides[]` entry.** The schema-coverage test cannot catch this — it walks `Config`, and `name` is not in `Config`. It is the one per-device field that is in the YAML but not in the UI, so it belongs to this decision.

### Requirements

1. **Allowlist a device from the device.** A one-click toggle on both `/devices` (per row) and `/devices/{mac}`, with the result visible immediately.
2. **Every per-device setting on the device page.** All five paths above — allowlist membership, the 8 `overrides[]` knobs, inactivity opt-in, reboot eligibility, and the `reboot.overrides[]` HA target — editable from one card on `/devices/{mac}`.
3. **No second source of truth.** The per-device card renders from the same `settings_schema` `FieldSpec`s the Settings page uses, so a future per-MAC field appears in both places or neither. No hand-copied labels, descriptions, or constraints.
4. **Surgical writes.** A per-device save changes only the keys it is actually editing; the rest of `config.yaml` is byte-identical, comments and all.
5. **Same fail-closed guarantee.** A per-device save validates the *whole* resulting config with the daemon's own parser and leaves the file untouched on rejection.
6. **Same auth posture.** The new route obeys the existing bearer-token middleware and stays CSRF-safe; the write fence stays an explicit allowlist, not a wildcard.
7. **No regression.** The Settings page keeps working; the daemon is untouched; existing suites stay green.

### Constraints

- The daemon gets **no new code** — this is entirely a sidecar concern, and the reload path it depends on already exists (ADR-0013 AC-6).
- `config.yaml` is comment-rich and live-only; a surgical write must preserve everything it does not touch.
- MACs must be quoted on write — pyyaml (YAML 1.1) reads an all-numeric MAC as a base-60 integer. `config_io._coerce_scalar` / `_coerce_list` already handle this with `DoubleQuotedScalarString`; the per-device path must go through them, not hand-roll.
- Two write paths now edit the same file. Their semantics must not diverge.

## Options Considered

### Option 1: Device-centric write route over a surgical raw-YAML mutation (Chosen)

**Description:** One new route, `POST /devices/{mac}/settings`, taking a partial JSON payload (absent key = unchanged). It reads the raw YAML (no interpolation, placeholders intact), mutates only the five per-MAC paths for that one MAC, validates the whole result with `validate_mapping`, and writes with the existing `write_config`. The `/devices/{mac}` card and the `/devices` row toggle are both clients of that one route.

**Pros:**
- Smallest possible diff to `config.yaml` — a toggle touches one list entry (Req 4).
- Reuses the entire validated-write pipeline: `validate_mapping`, `write_config`, per-field `_coerce_scalar`/`_coerce_list`, the bearer middleware, the daemon file-watch. Net-new logic is one mutation module.
- Rendering from `settings_schema` keeps Settings and the device card in lockstep (Req 3).
- Partial-payload semantics mean the row toggle and the full card are the same endpoint — one write path to secure and test, not two.

**Cons:**
- New mutation code (`upsert`/`remove` for MAC-keyed list entries) that the full-round-trip option would not need.
- Two entry points now write `config.yaml`; a shared helper is needed to keep coercion identical.
- Last-write-wins against a concurrently-open Settings page (see Risks).

### Option 2: Reuse the `/settings` round-trip pipeline verbatim

**Description:** Per-device save does `read_form_model` → mutate the model → `build_mapping` → `validate` → `write_config`.

**Pros:**
- Effectively zero new mutation code — the read/build pair already round-trips.
- Semantics provably identical to the Settings page, since it *is* the Settings page's code.

**Cons:**
- Materializes ~40 default keys into `config.yaml` on every toggle (measured). The operator's hand-curated file balloons the first time anyone clicks a checkbox, and the diff buries the actual change.
- Makes the `overrides[].name` deletion bug fire on a *device* save too — the surgical path avoids it structurally.
- A partial payload has no natural expression; the client would have to submit a whole config to change one boolean.

### Option 3: Generic config-patch endpoint (`POST /settings/patch`)

**Description:** A path-addressed partial-mutation API (`{"op": "add", "path": "allowlist", "value": "aa:bb:.."}`), with the device UI as its first consumer.

**Pros:**
- Reusable for any future partial edit; the device card becomes one caller among many.
- Naturally surgical.

**Cons:**
- A general mutation primitive is a much wider write surface than two pages need — any path, any op, from one endpoint. The `_assert_no_write_routes` fence degrades from "these specific things are writable" to "everything is, via one door."
- Needs its own path-expression grammar, and MAC-keyed list upserts do not express cleanly as index-addressed patches (`overrides[2].signal_dbm_max` breaks the moment a row is reordered by hand).
- Speculative generality: no second consumer is planned.

### Option 4: Keep it read-only; add deep links into Settings

**Description:** No new write path. The Allowlist column and the device page link to `/settings#allowlist` with the MAC pre-filled in the textarea via a query param.

**Pros:**
- Zero new write surface; ADR-0002's fence keeps its current shape.
- Trivial to build.

**Cons:**
- Does not meet Req 1 — it is still a navigation, a scroll, and a full-page Settings save (with its default-materializing side effect) to flip one boolean.
- The Settings page's own save then rewrites the whole managed surface anyway, so the "small change" framing is false.

## Decision

**Chosen Option:** Option 1 — a device-centric route (`POST /devices/{mac}/settings`) with partial-payload semantics, backed by a surgical raw-YAML mutation, rendered from the existing `settings_schema`.

**Rationale:** It is the only option that satisfies both Req 3 (one schema, no drift) and Req 4 (small diffs). Option 2's measured 40-key materialization makes a one-click toggle rewrite an operator's curated file, and it inherits the `overrides[].name` deletion bug on a code path where that bug would be new. Option 3 buys generality no planned consumer needs while dissolving the write fence that ADR-0013 was careful to keep narrow. Option 4 does not actually solve the problem. The chosen option adds exactly one route and one mutation module, and reuses the validator, the writer, the coercion rules, the auth middleware, and the daemon's file-watch unchanged.

**Forks resolved:**

- **Partial-payload semantics: absent key = unchanged.** The body carries only the settings being changed. `{"allowlisted": true}` from a row toggle and the card's full body are the same contract, so there is one endpoint, one auth check, one test surface. An explicit `null` clears a per-device override knob (inherit the global); omitting it leaves it alone. This distinction is the whole reason for partial semantics and is tested directly.
- **The MAC is the identity, taken from the path and normalized.** `POST /devices/{mac}/settings` normalizes with the daemon's own `reboot.normalize_mac` (already imported by `config.py:15`) and rejects anything failing `config._MAC_PATTERN` with `400` before touching the file. Membership tests and `overrides[].mac` matching are case-insensitive on the normalized form, so an operator's hand-typed uppercase MAC in `config.yaml` is matched, not duplicated. A MAC not present in the database is still editable — pre-configuring a device before it appears is legitimate.
- **Mutation is upsert/remove on MAC-keyed lists, in a shared module.** `device_config.py` owns: membership add/remove for the three MAC lists (`allowlist`, `detection.inactivity.macs`, `reboot.eligible`) and upsert/remove-by-MAC for the two object lists (`overrides[]`, `reboot.overrides[]`). An override row whose knobs are all cleared is **removed**, not left as a `{mac: ...}` stub. Every value goes through the existing `_coerce_scalar` / `_coerce_list` so MAC and time quoting stay identical to the Settings page.
- **Validate the whole config, not the fragment.** The mutated raw mapping is passed to `validate_mapping` in full, so cross-field rules still fire (a MAC that is both allowlisted and reboot-eligible, an all-null detection trio). Rejection returns the daemon's own message and leaves the file untouched.
- **`overrides[].name` becomes a real field.** It is added as a `FieldSpec` (`STRING`, optional, `restart_required: false`) documented as a label for humans that the daemon ignores. This fixes the verified Settings-page data loss *and* gives the device card its label input. `EXCLUDED_PATHS` stays empty; the coverage test is extended with the converse assertion — every `FieldSpec` path must resolve against `Config` **or** be on an explicit "cosmetic, not in Config" list — so the two directions of drift are both caught.
- **The write fence gains exactly one literal path.** `_ALLOWED_WRITE_PATHS` becomes `{"/settings", "/devices/{mac}/settings"}` — the route *template*, matching how Starlette reports `route.path`. Every other route stays GET-only, still enforced at startup. Auth and CSRF posture are inherited unchanged: JSON-only body (so a cross-site form POST cannot forge it) plus the header-carried bearer token.
- **Concurrency is last-write-wins, scoped.** The surgical path only replaces the keys it mutates, so a device toggle and a concurrent Settings save collide only if both touch the same per-MAC setting — a far smaller window than two full-file writes. No locking or ETag in this ADR; if it becomes a real problem the compare-on-save guard already contemplated in ADR-0013's risk table covers both writers at once.

## Acceptance Criteria

- [ ] **AC-1**: `POST /devices/{mac}/settings` with `{"allowlisted": true}` adds the normalized MAC to `allowlist:` in `config.yaml`; posting `{"allowlisted": false}` removes it; the request is idempotent (allowlisting an already-allowlisted MAC leaves the file byte-for-byte unchanged and does not duplicate the entry).
- [ ] **AC-2**: A per-device save writes **only** the keys it edits — given a config lacking `backoff:`, `safety_rails:`, and `reboot:`, a save that changes one per-device setting leaves those sections absent, preserves operator comments and `${VAR}` placeholders, and changes no unrelated line.
- [ ] **AC-3**: Partial semantics hold: a key absent from the payload leaves that setting unchanged, while an explicit `null` for a per-device override knob removes it (the device inherits the global). Clearing every knob of an `overrides[]` entry removes the entry rather than leaving a `{mac: ...}` stub.
- [ ] **AC-4**: The per-device card on `GET /devices/{mac}` renders every per-MAC field — `allowlist` membership, all `overrides[]` knobs, `detection.inactivity.macs` opt-in, `reboot.eligible` membership, and `reboot.overrides[]` name/`ha_entity` — sourced from `settings_schema` `FieldSpec`s (labels and descriptions included), pre-filled from the current `config.yaml`, with no per-field metadata duplicated in the template.
- [ ] **AC-5**: An invalid per-device save (malformed enum, out-of-range threshold, a value the daemon's cross-field rules reject) is refused with the daemon's own error message via `validate_mapping` on the **whole** mutated config, and `config.yaml` on disk is left byte-for-byte unchanged.
- [ ] **AC-6**: `_assert_no_write_routes` permits exactly `/settings` and `/devices/{mac}/settings`; a test asserts every other route is GET-only and that adding an unlisted write route still raises at startup. With `WIFI_SHEPARD_UI_TOKEN` set, a per-device save without a valid bearer token returns `401` and does not modify the file.
- [ ] **AC-7**: An unknown or malformed MAC in the path is rejected with `400` before any file access; a MAC that is valid but absent from the database is accepted and written (devices can be pre-configured).
- [ ] **AC-8**: `overrides[].name` survives a save from **both** write paths — a regression test asserts a full `POST /settings` round-trip no longer deletes a `name:` label from an existing `overrides[]` entry (the bug this ADR fixes), and that the device card can set it.
- [ ] **AC-9**: The `/devices` Allowlist column offers a per-row toggle that posts to the same route and reflects the new state on reload; with no token configured the page behaves as today for every read view, and the existing daemon + UI suites stay green.

## Consequences

### Positive

- The decision and the control finally live on the same page: the kick count that tells you a device is being harassed sits next to the toggle that stops it, with no MAC transcription in between.
- A per-device save produces a diff an operator can read — one list entry, not forty defaulted keys.
- The verified `overrides[].name` data-loss bug is fixed as a side effect, and the coverage test grows the converse assertion that would have caught it.
- No daemon change at all; the reload path built for ADR-0013 carries this for free.
- Rendering from `settings_schema` means the next per-MAC field added to `Config` shows up on the device card automatically.

### Negative

- Two routes now write `config.yaml`. They share the validator, writer, and coercion helpers, but "which page last wrote this key" is a genuinely new question when debugging.
- The write fence widens a second time. It is still an explicit two-entry allowlist, but ADR-0002's "read-only by construction" is now firmly historical.
- The per-device card duplicates *rendering* (not metadata) for a schema-driven form the Settings page already renders differently — a second Jinja macro set to keep visually coherent.
- Surgical mutation is new code with its own edge cases (case-insensitive MAC matching against hand-edited entries, empty-row pruning) that the round-trip option would have gotten for free.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Device save and Settings save race, one clobbers the other | Low | Low | Surgical writes collide only on the same per-MAC key, not the whole file; last-write-wins with the file as source of truth; ADR-0013's compare-on-save guard remains the escalation if it bites |
| Mutation logic drifts from the Settings page's coercion (MAC quoting, blank handling) | Medium | Medium | Both paths call the same `_coerce_scalar`/`_coerce_list`; a test asserts a device-written MAC and a Settings-written MAC serialize identically |
| A one-click toggle makes an accidental change easy | Medium | Low | Allowlisting is the *safe* direction (stops action); un-allowlisting is the risky one and is a deliberate second click with the result rendered on reload |
| Case-mismatched MAC in a hand-edited config yields a duplicate entry | Medium | Low | Normalize on read *and* compare case-insensitively before insert (AC-1 idempotence covers it) |
| Widening `_ALLOWED_WRITE_PATHS` becomes routine | Low | Medium | The fence stays a literal-path allowlist with a test that an unlisted write route raises at startup (AC-6); each addition needs an ADR, as here |
| `overrides[].name` implies the daemon uses it | Low | Low | The `FieldSpec` description states plainly that it is a label for humans and the daemon ignores it (`config.py:900` filters it out) |

## Implementation Plan

- [ ] **Phase 1 — schema fix.** Add the `overrides[].name` `FieldSpec`; extend `tests/ui/test_settings_schema_coverage_ac1.py` with the converse assertion (every `FieldSpec` path resolves against `Config` or is on an explicit cosmetic list). Regression test that a `/settings` round-trip preserves `name:` (AC-8).
- [ ] **Phase 2 — mutation module.** New `src/wifi_shepard_ui/device_config.py`: `read_device_settings(path, mac)` and `apply_device_settings(path, mac, payload)`, built on `_read_raw` / `_coerce_scalar` / `validate_mapping` / `write_config`. Unit tests for surgical diffs, idempotence, null-clears, empty-row pruning (AC-1, AC-2, AC-3, AC-5).
- [ ] **Phase 3 — route.** `POST /devices/{mac}/settings` in `app.py`; add the route template to `_ALLOWED_WRITE_PATHS`; MAC validation → `400`; auth/CSRF tests (AC-6, AC-7).
- [ ] **Phase 4 — device card.** Schema-driven per-device form on `history.html`, sharing the field-rendering macros with `settings.html` (AC-4).
- [ ] **Phase 5 — row toggle.** Allowlist toggle in the `/devices` table posting to the same route (AC-9).
- [ ] **Phase 6 — docs.** `README.md` + `CLAUDE.md` note the second write path; `config.example.yaml` comment for `overrides[].name`.

## Related ADRs

- [ADR-0013](./0013-settings-ui-write-paths.md) — the write pipeline, `${VAR}` secret handling, daemon file-watch reload, and the single-path write fence this **widens to two paths**. Its schema is the source of truth this reuses rather than duplicates.
- [ADR-0002](./0002-device-history-and-status-ui.md) — the read-only sidecar and the `/devices` + `/devices/{mac}` views this adds controls to. Its AC-6 no-write-routes fence, already amended once by ADR-0013, is amended again here.
- [ADR-0010](./0010-traffic-inactivity-detection.md) — `detection.inactivity.macs` is explicit per-MAC opt-in with no baseline learning, which is precisely why it needs a per-device control.
- [ADR-0005](./0005-device-identification-and-reboot-backend.md) / [ADR-0006](./0006-reboot-remediation.md) — `reboot.eligible` and `reboot.overrides[]`, the other two per-MAC lists surfaced on the card.
- [ADR-0007](./0007-action-policy-backoff-and-quiet-hours.md) — the `override > global` resolution the per-device knobs feed.

## References

- `src/wifi_shepard_ui/config_io.py` — `_read_raw`, `_coerce_scalar`, `_coerce_list`, `read_form_model`/`build_mapping` (the round-trip pair rejected as Option 2), `validate_mapping`, `write_config`/`_overlay`.
- `src/wifi_shepard_ui/settings_schema.py` — `FieldSpec`, `OBJECT_LISTS`, `item_fields`, `EXCLUDED_PATHS`.
- `src/wifi_shepard_ui/app.py:131` — `_assert_no_write_routes` / `_ALLOWED_WRITE_PATHS`.
- `src/wifi_shepard/config.py:900` — the `{k: v for k, v in o.items() if k in known}` filter that drops `overrides[].name`; `_MAC_PATTERN` and `reboot.normalize_mac` for MAC validation.
