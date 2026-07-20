"""Read / validate / write config.yaml for the settings UI (ADR-0013).

The UI is a *view over* config.yaml (the "syncs with the YAML" model): it reads the
live file to pre-fill the form and writes edits back to that same file. Three rules
this module enforces:

- **Secrets never enter the UI.** A secret field stores a ``${NAME}`` env-var
  placeholder; the UI only ever sees/writes the *name*, never the value
  (``_env_name`` refuses to surface anything that isn't a placeholder).
- **Validate with the daemon's own parser.** A proposed config is checked with
  ``wifi_shepard.config.build_config_from_mapping`` (no interpolation — secrets stay
  ``${NAME}`` literals), so the UI can't persist a config the daemon would reject.
- **Round-trip preserves the file.** Writes go through ``ruamel.yaml`` (comments, key
  order, and ``${VAR}`` placeholders preserved) and land atomically (temp + rename).
"""

from __future__ import annotations

import os
import re
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from wifi_shepard.config import build_config_from_mapping
from wifi_shepard_ui import settings_schema as ss

_ENV_REF = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")

# Top-level config keys the schema manages. On save, a managed key absent from the
# proposed config is removed from the file (e.g. the operator disabled quiet_hours);
# unmanaged keys an operator added by hand are left untouched.
_MANAGED_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "detection",
        "scanner",
        "backoff",
        "safety_rails",
        "quiet_hours",
        "reboot",
        "overrides",
        "allowlist",
        "controllers",
        "home_assistant",
        "dns_sources",
    }
)


def _env_name(value: Any) -> str:
    """Return the env var name from a ``${NAME}`` placeholder, else "".

    Anything that is not a well-formed placeholder returns "" — so a literal secret
    that somehow landed in the file is never surfaced to the browser (AC-3).
    """
    if isinstance(value, str):
        m = _ENV_REF.match(value.strip())
        if m:
            return m.group(1)
    return ""


# --------------------------------------------------------------------------- reading


def _read_raw(path: Path) -> dict[str, Any]:
    """Load config.yaml as a plain dict WITHOUT interpolating ``${VAR}`` (so secret
    placeholders stay intact). Missing/empty file -> {} (fresh-deploy empty state)."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


# Distinguishes an absent key (show the field's default) from a present ``null``
# (the operator disabled/cleared it — show blank).
_MISSING = object()


def _get(raw: dict[str, Any], dotted: str) -> Any:
    node: Any = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _scalar_fields() -> tuple[ss.FieldSpec, ...]:
    return tuple(
        f
        for f in ss.FIELDS
        if "[]" not in f.path
        and f.kind not in (ss.Kind.INT_LIST, ss.Kind.STRING_LIST, ss.Kind.MAC_LIST)
    )


def _scalar_list_fields() -> tuple[ss.FieldSpec, ...]:
    return tuple(
        f
        for f in ss.FIELDS
        if "[]" not in f.path
        and f.kind in (ss.Kind.INT_LIST, ss.Kind.STRING_LIST, ss.Kind.MAC_LIST)
    )


def _display_scalar(field: ss.FieldSpec, raw_value: Any) -> Any:
    """Value to pre-fill an input with. Secrets -> env var name; an absent key ->
    the field's default; a present ``null`` -> blank (disabled/inherit/cleared)."""
    if field.secret:
        return "" if raw_value is _MISSING else _env_name(raw_value)
    if raw_value is _MISSING:
        return "" if field.default is None else field.default
    if raw_value is None:
        return ""
    return raw_value


def _item_leaf(prefix: str, item: dict[str, Any]) -> dict[str, Any]:
    """Pre-fill values for one object-list row, keyed by leaf field name."""
    out: dict[str, Any] = {}
    for f in ss.FIELDS:
        if not f.path.startswith(prefix):
            continue
        leaf = f.path[len(prefix) :]
        if "[]" in leaf:  # a nested list (dns_sources instances) handled by caller
            continue
        raw_value = item.get(leaf, _MISSING)
        out[leaf] = _display_scalar(f, raw_value)
    return out


def read_allowlist(path: Path) -> set[str]:
    """The allowlist MACs from config.yaml, lowercased for the UI's case-insensitive
    match (ADR-0013 AC-8 — the authoritative list, replacing the old parallel env).
    Missing file / no allowlist -> empty set."""
    raw = _read_raw(path)
    entries = raw.get("allowlist")
    if not isinstance(entries, list):
        return set()
    return {str(m).strip().lower() for m in entries if isinstance(m, (str, int)) and str(m).strip()}


def read_form_model(path: Path) -> dict[str, Any]:
    """Build the pre-fill model the settings template renders from (AC-2/AC-3/AC-10)."""
    raw = _read_raw(path)

    scalars: dict[str, Any] = {}
    for f in _scalar_fields():
        scalars[f.path] = _display_scalar(f, _get(raw, f.yaml_path or f.path))

    scalar_lists: dict[str, list[Any]] = {}
    for f in _scalar_list_fields():
        value = _get(raw, f.path)
        if isinstance(value, list):
            scalar_lists[f.path] = list(value)
        else:
            scalar_lists[f.path] = list(f.default or ())

    object_lists: dict[str, list[dict[str, Any]]] = {}
    for spec in ss.OBJECT_LISTS:
        node: Any = raw
        for part in spec.location[:-1]:
            node = node.get(part, {}) if isinstance(node, dict) else {}
        items = node.get(spec.location[-1]) if isinstance(node, dict) else None
        rows: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            row = _item_leaf(spec.item_prefix, item)
            if spec.nested_key is not None and spec.nested_prefix is not None:
                nested_items = item.get(spec.nested_key) or []
                row[spec.nested_key] = [
                    _item_leaf(spec.nested_prefix, ni)
                    for ni in nested_items
                    if isinstance(ni, dict)
                ]
            rows.append(row)
        object_lists[spec.key] = rows

    # An optional block is "on" iff it is present (and non-null) in the file.
    section_enabled = {}
    for o in ss.OPTIONAL_SECTIONS:
        val = _get(raw, o.path)
        section_enabled[o.path] = val is not _MISSING and val is not None

    return {
        "scalars": scalars,
        "scalar_lists": scalar_lists,
        "object_lists": object_lists,
        "section_enabled": section_enabled,
        "config_exists": bool(raw),
    }


# --------------------------------------------------------------------- building/coerce


class CoercionError(ValueError):
    """A submitted value can't be coerced to the field's type (bad int, etc.)."""


def _coerce_scalar(field: ss.FieldSpec, value: Any) -> Any:
    """Coerce a raw submitted value to the Python type the config loader expects,
    or a sentinel ``_OMIT`` to drop the key. Raises CoercionError on a bad number."""
    kind = field.kind
    if kind is ss.Kind.SECRET_REF:
        name = str(value).strip() if value is not None else ""
        if not name:
            return _OMIT
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
            raise CoercionError(
                f"{field.path}: '{name}' is not a valid environment variable name "
                f"(use UPPER_CASE letters, digits, and underscores)"
            )
        return f"${{{name}}}"
    if kind is ss.Kind.BOOL:
        return bool(value)
    if kind in (ss.Kind.INT, ss.Kind.INT_OR_NULL):
        if value is None or (isinstance(value, str) and value.strip() == ""):
            if kind is ss.Kind.INT_OR_NULL:
                return None if field.blank_writes_null else _OMIT
            return _OMIT  # a blank plain-int field: leave the loader's default
        try:
            return int(str(value).strip())
        except ValueError as exc:
            raise CoercionError(f"{field.path}: '{value}' is not a whole number") from exc
    if kind is ss.Kind.ENUM:
        s = str(value).strip() if value is not None else ""
        return s if s else _OMIT
    # STRING / MAC / TIME_HHMM / TIMEZONE
    s = str(value).strip() if value is not None else ""
    # A required string is kept even when empty so build_config raises its own clear
    # "is required" error; an optional one is dropped.
    if not s and not _string_required(field):
        return _OMIT
    if s and kind in (ss.Kind.TIME_HHMM, ss.Kind.MAC):
        # Force-quote on write: pyyaml (the daemon's YAML-1.1 reader) parses an
        # unquoted "23:00" as the sexagesimal int 1380, and an all-numeric MAC as a
        # base-60 int. Quoting keeps them strings. (str subclass — validation is
        # unaffected.)
        return DoubleQuotedScalarString(s)
    return s


_OMIT = object()

_REQUIRED_STRING_PATHS: frozenset[str] = frozenset(
    {
        "controllers[].type",
        "controllers[].name",
        "controllers[].host",
        "controllers[].username",
        "home_assistant.url",
        "home_assistant.notify_service",
        "reboot.overrides[].mac",
        "overrides[].mac",
        "dns_sources[].instances[].url",
        "quiet_hours.start",
        "quiet_hours.end",
        "quiet_hours.timezone",
    }
)


def _string_required(field: ss.FieldSpec) -> bool:
    return field.path in _REQUIRED_STRING_PATHS


def _coerce_list(field: ss.FieldSpec, values: Any) -> list[Any]:
    out: list[Any] = []
    for v in values or []:
        s = str(v).strip()
        if not s:
            continue
        if field.kind is ss.Kind.INT_LIST:
            try:
                out.append(int(s))
            except ValueError as exc:
                raise CoercionError(f"{field.path}: '{s}' is not a whole number") from exc
        elif field.kind is ss.Kind.MAC_LIST:
            # Quote all-numeric MACs so pyyaml doesn't read them as base-60 ints.
            out.append(DoubleQuotedScalarString(s))
        else:
            out.append(s)
    return out


def _set_path(mapping: dict[str, Any], dotted: str, value: Any) -> None:
    if value is _OMIT:
        return
    node = mapping
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _build_item(prefix: str, row: dict[str, Any], nested: tuple[str, str] | None) -> dict[str, Any]:
    item: dict[str, Any] = {}
    for f in ss.FIELDS:
        if not f.path.startswith(prefix):
            continue
        leaf = f.path[len(prefix) :]
        if "[]" in leaf:
            continue
        coerced = _coerce_scalar(f, row.get(leaf))
        if coerced is not _OMIT:
            item[leaf] = coerced
    if nested is not None:
        nested_key, nested_prefix = nested
        item[nested_key] = [
            _build_item(nested_prefix, ni, None) for ni in (row.get(nested_key) or [])
        ]
    return item


def build_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    """Assemble a config-shaped mapping from the submitted form payload.

    Coerces every value per its schema kind, wraps secrets as ``${NAME}``, and drops
    inherit/keep blanks — producing exactly the dict shape ``build_config_from_mapping``
    expects (as if it came from ``yaml.safe_load``).
    """
    scalars = payload.get("scalars") or {}
    scalar_lists = payload.get("scalar_lists") or {}
    object_lists = payload.get("object_lists") or {}

    mapping: dict[str, Any] = {}

    for f in _scalar_fields():
        _set_path(mapping, f.yaml_path or f.path, _coerce_scalar(f, scalars.get(f.path)))

    for f in _scalar_list_fields():
        _set_path(mapping, f.path, _coerce_list(f, scalar_lists.get(f.path)))

    for spec in ss.OBJECT_LISTS:
        rows = object_lists.get(spec.key) or []
        nested = (
            (spec.nested_key, spec.nested_prefix)
            if spec.nested_key is not None and spec.nested_prefix is not None
            else None
        )
        built = [_build_item(spec.item_prefix, r, nested) for r in rows if isinstance(r, dict)]
        node = mapping
        for part in spec.location[:-1]:
            node = node.setdefault(part, {})
        node[spec.location[-1]] = built

    # Optional blocks are enabled only by their explicit per-section toggle — so a
    # round-trip save never activates a block that was off just because its fields carry
    # non-empty defaults, and never writes detection.dns_thrash (which requires
    # dns_sources) uninvited. An absent toggle (older client) falls back to the
    # "is there any content?" heuristic.
    enabled = payload.get("section_enabled")
    for o in ss.OPTIONAL_SECTIONS:
        if isinstance(enabled, dict) and o.path in enabled:
            drop = not enabled[o.path]
        else:
            drop = _section_is_empty(_get_from(mapping, o.path))
        if drop:
            _del_path(mapping, o.path)

    return mapping


def _get_from(mapping: dict[str, Any], dotted: str) -> Any:
    node: Any = mapping
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _del_path(mapping: dict[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    node: Any = mapping
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _section_is_empty(section: Any) -> bool:
    """A section counts as 'off' when it has no meaningful content. For quiet_hours /
    home_assistant this is an empty/all-blank mapping; for dns_sources an empty list."""
    if section is None:
        return True
    if isinstance(section, list):
        return len(section) == 0
    if isinstance(section, dict):
        return all(v in (None, "", {}, []) for v in section.values())
    return False


def validate_mapping(mapping: dict[str, Any]) -> None:
    """Raise ValueError (the daemon's own message) if the mapping is invalid (AC-4)."""
    build_config_from_mapping(mapping)


# --------------------------------------------------------------------------- writing


def round_trip_yaml() -> YAML:
    """The shared ruamel configuration — preserves comments, key order, and quoting.
    Used by both writers (whole-file settings save and per-device edits, ADR-0014)."""
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.width = 4096  # don't line-wrap long scalars (URLs, descriptions)
    return yaml_rt


def dump_to_string(doc: Any, yaml_rt: YAML | None = None) -> str:
    """Serialize a (possibly comment-carrying) document to YAML text."""
    buf = StringIO()
    (yaml_rt or round_trip_yaml()).dump(doc, buf)
    return buf.getvalue()


def write_text_atomic(path: Path, text: str) -> None:
    """Temp file + rename, so a crashed write never leaves a truncated config the
    daemon might reload."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_config(path: Path, mapping: dict[str, Any]) -> None:
    """Atomically write the mapping to config.yaml, preserving comments / key order /
    ``${VAR}`` placeholders of an existing file (AC-5). A managed section absent from
    the mapping is removed; unmanaged operator keys are left untouched.
    """
    yaml_rt = round_trip_yaml()

    if path.exists():
        with path.open("r") as fh:
            doc = yaml_rt.load(fh)
        if not isinstance(doc, CommentedMap):
            doc = CommentedMap()
        _overlay(doc, mapping)
        for key in _MANAGED_TOP_LEVEL:
            if key not in mapping and key in doc:
                del doc[key]
    else:
        doc = mapping

    write_text_atomic(path, dump_to_string(doc, yaml_rt))


def _overlay(dst: Any, src: dict[str, Any]) -> None:
    """Set each key of ``src`` into ``dst``, recursing into nested mappings so a key's
    surrounding comments survive. Lists and scalars are replaced wholesale."""
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _overlay(dst[key], value)
        else:
            dst[key] = value
