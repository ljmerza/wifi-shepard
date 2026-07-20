"""Per-device (per-MAC) config edits for the device pages (ADR-0014).

The surgical counterpart to :mod:`config_io`'s whole-file settings save. Where that
module rebuilds every managed key from a form model — materializing schema defaults
along the way — this one loads the YAML *document*, mutates only the per-MAC nodes
named in the payload, and leaves every other node (and its comments, quoting, and
flow style) exactly as the operator wrote it.

Three rules it shares with the settings save:

- **Validate with the daemon's own parser.** The whole mutated config goes through
  ``config_io.validate_mapping``, so cross-field rules still fire and the UI cannot
  persist something the daemon would reject.
- **Coerce through the schema.** Every value passes ``config_io.coerce_scalar`` for
  its ``FieldSpec``, so MAC quoting and blank handling match the settings page byte
  for byte.
- **Write atomically.** Temp file + rename, via ``config_io.write_text_atomic`` — and
  only when something actually changed.

Payload shape (every key optional — an absent key leaves that setting alone)::

    {
      "allowlisted": bool,          # }
      "inactivity_watched": bool,   # } ss.PER_DEVICE_MEMBERSHIPS
      "reboot_eligible": bool,      # }
      "overrides": {"signal_dbm_max": -65, "tx_rate_kbps_max": null, ...},
      "reboot_override": {"name": "...", "ha_entity": "switch...."},
    }

Within an object-list payload, ``null`` (or "") clears that knob so the device
inherits the global; omitting it leaves the stored value untouched. An unrecognized
key is an error, not a silent no-op — a typo must not report success.

The device's identity is the URL, never the payload: ``mac`` is rejected as an
editable leaf so a request for one device can't re-point another device's row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

# The daemon's own MAC pattern, imported rather than restated so the two can't drift.
from wifi_shepard.config import _MAC_PATTERN
from wifi_shepard.reboot import normalize_mac
from wifi_shepard_ui import config_io
from wifi_shepard_ui import settings_schema as ss

# Marks a leaf the payload asked to remove (an explicit null / blank), as distinct
# from one it didn't mention.
_CLEAR = object()


def is_valid_mac(mac: str) -> bool:
    """True for a canonical ``aa:bb:cc:dd:ee:ff`` MAC, in any case."""
    return bool(_MAC_PATTERN.match(mac.strip()))


def payload_keys() -> frozenset[str]:
    """Every key this module accepts at the top level of a payload."""
    return frozenset(
        [m.key for m in ss.PER_DEVICE_MEMBERSHIPS] + [k for k, _ in ss.PER_DEVICE_OBJECT_LISTS]
    )


def _node(doc: Any, parts: tuple[str, ...]) -> Any:
    """Walk to ``parts`` without creating anything. Missing -> None."""
    node = doc
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _ensure_parent(doc: CommentedMap, parts: tuple[str, ...]) -> CommentedMap:
    """Walk to the parent mapping of ``parts``, creating empty blocks as needed."""
    node: Any = doc
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = CommentedMap()
        node = node[part]
    return node


def _mac_matches(value: Any, mac: str) -> bool:
    return isinstance(value, str) and normalize_mac(value) == mac


def _append_keeping_trailing_comment(seq: CommentedSeq, value: Any) -> None:
    """Append to a block sequence without swallowing the comment that follows it.

    ruamel attaches a comment block sitting *after* the last item to that item, so a
    plain ``append`` lands the new entry below it — pushing the next section's header
    comment into the middle of this list. Move the trailing comment onto the new last
    item so it stays at the bottom where the operator wrote it.
    """
    previous_last = len(seq) - 1
    seq.append(value)
    comments = getattr(seq, "ca", None)
    if comments is not None and previous_last in comments.items:
        comments.items[len(seq) - 1] = comments.items.pop(previous_last)


def _apply_membership(doc: CommentedMap, spec: ss.MembershipSpec, mac: str, on: bool) -> bool:
    """Add/remove ``mac`` from the MAC list at ``spec.path``. Returns True if changed."""
    parts = tuple(spec.path.split("."))
    existing = _node(doc, parts)
    # A present-but-wrong-shaped node (`allowlist: null`, or a scalar typo) is the
    # operator's data. Refuse rather than silently replacing it with a fresh list.
    if existing is not None and not isinstance(existing, list):
        raise ValueError(f"'{spec.path}' in config.yaml is not a list — fix it by hand first")

    if on:
        if isinstance(existing, list) and any(_mac_matches(v, mac) for v in existing):
            return False  # already a member — idempotent
        if existing is None:
            parent = _ensure_parent(doc, parts)
            parent[parts[-1]] = CommentedSeq()
            existing = parent[parts[-1]]
        # Quoted so pyyaml (YAML 1.1) can't read an all-numeric MAC as a base-60 int.
        _append_keeping_trailing_comment(existing, DoubleQuotedScalarString(mac))
        return True

    if not isinstance(existing, list):
        return False
    doomed = [i for i, v in enumerate(existing) if _mac_matches(v, mac)]
    for i in reversed(doomed):
        del existing[i]
    return bool(doomed)


def _apply_object_row(doc: CommentedMap, key: str, prefix: str, mac: str, values: Any) -> bool:
    """Upsert this MAC's row in the object list at ``prefix``. Returns True if changed.

    A row whose last real field is cleared is deleted rather than kept as a stub.
    """
    if not isinstance(values, dict):
        raise ValueError(f"'{key}' must be an object of field names to values")
    spec = ss.object_list_by_prefix(prefix)
    if spec is None:  # pragma: no cover - guarded by the schema
        raise ValueError(f"unknown per-device section '{key}'")

    # Coerce everything up front so an invalid value aborts before the document is
    # touched, and so `mac` is refused whether or not the row already exists.
    pending: dict[str, Any] = {}
    for leaf, value in values.items():
        if leaf == "mac":
            raise ValueError(
                f"'{key}.mac' is not editable — a device's identity is the URL it was posted to"
            )
        field = ss.field_by_path(f"{prefix}{leaf}")
        if field is None:
            raise ValueError(f"'{leaf}' is not a per-device setting of '{key}'")
        if value is None:
            pending[leaf] = _CLEAR
            continue
        coerced = config_io.coerce_scalar(field, value)
        pending[leaf] = _CLEAR if coerced is config_io.OMIT else coerced

    location = tuple(spec.location)
    seq = _node(doc, location)
    # Same rule as _apply_membership: a present-but-wrong-shaped node is the operator's
    # data, not ours to replace.
    if seq is not None and not isinstance(seq, list):
        raise ValueError(f"'{'.'.join(location)}' in config.yaml is not a list — fix it by hand")
    row = None
    if isinstance(seq, list):
        row = next(
            (r for r in seq if isinstance(r, dict) and _mac_matches(r.get("mac"), mac)), None
        )

    changed = False
    if row is None:
        # Nothing to create if the payload only clears things (or is empty).
        if all(v is _CLEAR for v in pending.values()):
            return False
        row = CommentedMap()
        row["mac"] = DoubleQuotedScalarString(mac)
        if not isinstance(seq, list):
            parent = _ensure_parent(doc, location)
            parent[location[-1]] = CommentedSeq()
            seq = parent[location[-1]]
        _append_keeping_trailing_comment(seq, row)
        changed = True

    cleared_any = False
    for leaf, value in pending.items():
        if value is _CLEAR:
            if leaf in row:
                del row[leaf]
                changed = cleared_any = True
        elif row.get(leaf) != value:
            row[leaf] = value
            changed = True

    # A row carrying only its identity says nothing — drop it. Only when *this* call
    # emptied it, so an operator's hand-written `- mac: xx` stub survives a no-op.
    if cleared_any and set(row) <= {"mac"}:
        seq.remove(row)
        changed = True
    return changed


def read_device_settings(path: Path, mac: str) -> dict[str, Any]:
    """The per-MAC slice of config.yaml, shaped for the device card's pre-fill."""
    mac = normalize_mac(mac)
    raw = config_io.read_raw(path)

    model: dict[str, Any] = {"mac": mac}
    for membership in ss.PER_DEVICE_MEMBERSHIPS:
        entries = _node(raw, tuple(membership.path.split(".")))
        model[membership.key] = isinstance(entries, list) and any(
            _mac_matches(v, mac) for v in entries
        )
        # Whether the global feature this membership feeds is switched on. The card
        # says so, rather than offering a toggle that silently does nothing.
        if membership.gated_by:
            model[f"{membership.key}__gate_on"] = bool(
                _node(raw, tuple(membership.gated_by.split(".")))
            )

    for key, prefix in ss.PER_DEVICE_OBJECT_LISTS:
        spec = ss.object_list_by_prefix(prefix)
        seq = _node(raw, tuple(spec.location)) if spec is not None else None
        row: dict[str, Any] = {}
        if isinstance(seq, list):
            match = next(
                (r for r in seq if isinstance(r, dict) and _mac_matches(r.get("mac"), mac)), None
            )
            row = dict(match) if match else {}
        model[key] = {
            field.path[len(prefix) :]: row.get(field.path[len(prefix) :], "")
            for field in ss.item_fields(prefix)
            if field.path != f"{prefix}mac"
        }
    return model


def apply_device_settings(path: Path, mac: str, payload: dict[str, Any]) -> bool:
    """Apply a partial per-device payload to config.yaml. Returns True if the file was
    rewritten. Raises ValueError if the payload is unrecognized or the resulting config
    would be invalid."""
    mac = normalize_mac(mac)
    unknown = sorted(set(payload) - payload_keys())
    if unknown:
        raise ValueError(
            f"unrecognized per-device setting(s): {', '.join(unknown)} "
            f"(expected one of: {', '.join(sorted(payload_keys()))})"
        )

    before = path.read_text() if path.exists() else ""
    yaml_rt = config_io.round_trip_yaml(before)
    doc = yaml_rt.load(before) if before.strip() else None
    if not isinstance(doc, CommentedMap):
        doc = CommentedMap()

    touched = False
    for membership in ss.PER_DEVICE_MEMBERSHIPS:
        if membership.key in payload:
            touched |= _apply_membership(doc, membership, mac, bool(payload[membership.key]))
    for key, prefix in ss.PER_DEVICE_OBJECT_LISTS:
        if key in payload:
            touched |= _apply_object_row(doc, key, prefix, mac, payload[key])

    # Nothing changed: never rewrite. Re-emitting an untouched document would reflow
    # whatever ruamel doesn't round-trip byte-for-byte, so a no-op POST must not write.
    if not touched:
        return False

    # Validate the WHOLE config, not just the edited fragment, so cross-field rules
    # still fire.
    #
    # Validate the *text about to be written*, re-read with pyyaml — the daemon's own
    # reader. Handing the ruamel document straight to the validator would check a
    # YAML 1.2 parse of a file the daemon loads as YAML 1.1, and the two disagree in
    # both directions: `dry_run: yes` is True to the daemon but the string "yes" to
    # ruamel (a valid config wrongly rejected), and an unquoted all-numeric MAC is a
    # string to ruamel but a base-60 int to the daemon (an invalid config wrongly
    # accepted, breaking the fail-closed guarantee).
    text = config_io.dump_to_string(doc, yaml_rt)
    config_io.validate_mapping(yaml.safe_load(text) or {})

    config_io.write_text_atomic(path, text)
    return True
