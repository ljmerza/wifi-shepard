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
- **Coerce through the schema.** Every value passes ``config_io._coerce_scalar`` for
  its ``FieldSpec``, so MAC quoting and blank handling match the settings page byte
  for byte.
- **Write atomically.** Temp file + rename, via ``config_io.write_text_atomic``.

Payload shape (every key optional — an absent key leaves that setting alone)::

    {
      "allowlisted": bool,          # }
      "inactivity_watched": bool,   # } ss.PER_DEVICE_MEMBERSHIPS
      "reboot_eligible": bool,      # }
      "overrides": {"signal_dbm_max": -65, "tx_rate_kbps_max": null, ...},
      "reboot_override": {"name": "...", "ha_entity": "switch...."},
    }

Within an object-list payload, ``null`` (or "") clears that knob so the device
inherits the global; omitting it leaves the stored value untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

# The daemon's own MAC pattern, imported rather than restated so the two can't drift.
from wifi_shepard.config import _MAC_PATTERN
from wifi_shepard.reboot import normalize_mac
from wifi_shepard_ui import config_io
from wifi_shepard_ui import settings_schema as ss


def is_valid_mac(mac: str) -> bool:
    """True for a canonical ``aa:bb:cc:dd:ee:ff`` MAC, in any case."""
    return bool(_MAC_PATTERN.match(mac.strip()))


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


def _apply_membership(doc: CommentedMap, spec: ss.MembershipSpec, mac: str, on: bool) -> bool:
    """Add/remove ``mac`` from the MAC list at ``spec.path``. Returns True if changed."""
    parts = tuple(spec.path.split("."))
    existing = _node(doc, parts)

    if on:
        if isinstance(existing, list) and any(_mac_matches(v, mac) for v in existing):
            return False  # already a member — idempotent
        if not isinstance(existing, list):
            parent = _ensure_parent(doc, parts)
            parent[parts[-1]] = CommentedSeq()
            existing = parent[parts[-1]]
        # Quoted so pyyaml (YAML 1.1) can't read an all-numeric MAC as a base-60 int.
        existing.append(DoubleQuotedScalarString(mac))
        return True

    if not isinstance(existing, list):
        return False
    doomed = [i for i, v in enumerate(existing) if _mac_matches(v, mac)]
    for i in reversed(doomed):
        del existing[i]
    return bool(doomed)


def _apply_object_row(doc: CommentedMap, key: str, prefix: str, mac: str, values: Any) -> bool:
    """Upsert this MAC's row in the object list at ``prefix``. Returns True if changed.

    A row left with nothing but its ``mac`` is deleted rather than kept as a stub.
    """
    if not isinstance(values, dict):
        raise ValueError(f"'{key}' must be an object of field names to values")
    spec = ss.object_list_by_prefix(prefix)
    if spec is None:  # pragma: no cover - guarded by the schema
        raise ValueError(f"unknown per-device section '{key}'")

    location = tuple(spec.location)
    seq = _node(doc, location)
    row = None
    if isinstance(seq, list):
        row = next(
            (r for r in seq if isinstance(r, dict) and _mac_matches(r.get("mac"), mac)), None
        )

    changed = False
    pending: dict[str, Any] = {}
    for leaf, value in values.items():
        field = ss.field_by_path(f"{prefix}{leaf}")
        if field is None:
            raise ValueError(f"'{leaf}' is not a per-device setting of '{key}'")
        if value is None:
            pending[leaf] = _CLEAR
            continue
        coerced = config_io._coerce_scalar(field, value)
        pending[leaf] = _CLEAR if coerced is config_io._OMIT else coerced

    if row is None:
        # Nothing to create if the whole payload is clears.
        if all(v is _CLEAR for v in pending.values()):
            return False
        row = CommentedMap()
        row["mac"] = DoubleQuotedScalarString(mac)
        if not isinstance(seq, list):
            parent = _ensure_parent(doc, location)
            parent[location[-1]] = CommentedSeq()
            seq = parent[location[-1]]
        seq.append(row)
        changed = True

    for leaf, value in pending.items():
        if value is _CLEAR:
            if leaf in row:
                del row[leaf]
                changed = True
        elif row.get(leaf) != value:
            row[leaf] = value
            changed = True

    # A row carrying only its identity says nothing — drop it.
    if set(row) <= {"mac"}:
        seq.remove(row)
        changed = True
    return changed


_CLEAR = object()


def read_device_settings(path: Path, mac: str) -> dict[str, Any]:
    """The per-MAC slice of config.yaml, shaped for the device card's pre-fill."""
    mac = normalize_mac(mac)
    raw = config_io._read_raw(path)

    model: dict[str, Any] = {"mac": mac}
    for membership in ss.PER_DEVICE_MEMBERSHIPS:
        entries = _node(raw, tuple(membership.path.split(".")))
        model[membership.key] = isinstance(entries, list) and any(
            _mac_matches(v, mac) for v in entries
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
    """Apply a partial per-device payload to config.yaml. Returns True if the file
    changed. Raises ValueError if the resulting config would be invalid."""
    mac = normalize_mac(mac)
    yaml_rt = config_io.round_trip_yaml()

    before = path.read_text() if path.exists() else ""
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

    # Validate the WHOLE config, not just the edited fragment, so cross-field rules
    # still fire. CommentedMap/CommentedSeq are dict/list subclasses, so the daemon's
    # parser reads them unchanged.
    config_io.validate_mapping(doc)

    after = config_io.dump_to_string(doc, yaml_rt)
    if after == before:
        return False  # nothing to do — keeps a repeat toggle byte-for-byte idempotent
    config_io.write_text_atomic(path, after)
    return touched or True
