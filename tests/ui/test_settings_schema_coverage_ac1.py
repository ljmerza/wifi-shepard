"""ADR-0013 AC-1: the settings schema covers every editable field in Config.

Walks ``wifi_shepard.config.Config`` recursively and asserts every reachable leaf
field is either described by the schema or on the explicit exclusion list — so a
new config field can't silently become un-editable. Also asserts the schema has no
stale/bogus paths and that every required attribute is populated.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import types
import typing

import pytest

from wifi_shepard.config import Config
from wifi_shepard_ui import settings_schema as ss


def _strip_optional(tp: object) -> object:
    """``X | None`` / ``Optional[X]`` -> ``X`` (only when exactly one non-None arm)."""
    if typing.get_origin(tp) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def _seq_elem(tp: object) -> object | None:
    """Element type of ``tuple[X, ...]`` / ``list[X]`` / ``Sequence[X]``, else None."""
    if typing.get_origin(tp) in (tuple, list, cabc.Sequence):
        args = typing.get_args(tp)
        if args:
            return args[0]
    return None


def _leaf_paths(cls: type, prefix: str = "") -> list[str]:
    """Every editable leaf path under a dataclass. Nested dataclasses recurse with
    a dotted path; sequences of dataclasses recurse with a ``[]`` segment; scalars
    and sequences of scalars are leaves."""
    hints = typing.get_type_hints(cls)
    out: list[str] = []
    for f in dataclasses.fields(cls):
        tp = _strip_optional(hints[f.name])
        path = f"{prefix}{f.name}"
        if dataclasses.is_dataclass(tp):
            out += _leaf_paths(tp, f"{path}.")
            continue
        elem = _seq_elem(tp)
        if elem is not None and dataclasses.is_dataclass(elem):
            out += _leaf_paths(elem, f"{path}[].")
        else:
            out.append(path)
    return out


def test_ac_1_every_config_field_is_covered_or_excluded():
    leaves = set(_leaf_paths(Config))
    covered = set(ss.covered_paths())
    excluded = set(ss.EXCLUDED_PATHS)

    missing = leaves - covered - excluded
    assert not missing, (
        f"config fields not editable in the settings UI (add to FIELDS or EXCLUDED_PATHS): "
        f"{sorted(missing)}"
    )


def test_ac_1_schema_has_no_bogus_paths():
    leaves = set(_leaf_paths(Config))
    covered = set(ss.covered_paths())
    bogus = covered - leaves - set(ss.COSMETIC_PATHS)
    assert not bogus, (
        "schema describes paths that don't exist in Config (add to COSMETIC_PATHS only if "
        f"the daemon deliberately ignores them): {sorted(bogus)}"
    )


def test_ac_1_cosmetic_paths_are_real_fields_absent_from_config():
    """ADR-0014's converse assertion: a cosmetic path must be a real editable field AND
    genuinely absent from Config — otherwise it belongs in the normal coverage check."""
    leaves = set(_leaf_paths(Config))
    covered = set(ss.covered_paths())
    for path in ss.COSMETIC_PATHS:
        assert path in covered, f"COSMETIC_PATHS lists {path}, which has no FieldSpec"
        assert path not in leaves, (
            f"{path} exists in Config — drop it from COSMETIC_PATHS so it is covered normally"
        )


def test_ac_1_excluded_paths_are_real_leaves():
    leaves = set(_leaf_paths(Config))
    stale = set(ss.EXCLUDED_PATHS) - leaves
    assert not stale, f"EXCLUDED_PATHS references non-existent Config fields: {sorted(stale)}"


def test_ac_1_no_duplicate_field_paths():
    paths = [f.path for f in ss.FIELDS]
    dupes = {p for p in paths if paths.count(p) > 1}
    assert not dupes, f"duplicate FieldSpec paths: {sorted(dupes)}"


def test_ac_1_every_field_has_label_and_description():
    for f in ss.FIELDS:
        assert f.label.strip(), f"{f.path} has an empty label"
        # Descriptions are the whole point of Phase 1 — hold them to a real length.
        assert len(f.description.strip()) >= 20, f"{f.path} has a too-short description"
        assert f.section, f"{f.path} has no section"


def test_ac_1_enum_fields_declare_choices():
    for f in ss.FIELDS:
        if f.kind is ss.Kind.ENUM:
            assert f.choices, f"enum field {f.path} must declare choices"


def test_ac_1_secret_fields_are_secret_ref_kind():
    for f in ss.FIELDS:
        assert f.secret == (f.kind is ss.Kind.SECRET_REF), (
            f"{f.path}: secret flag and SECRET_REF kind must agree"
        )


@pytest.mark.parametrize(
    "path",
    [
        "controllers[].password",
        "home_assistant.token",
        "dns_sources[].password",
    ],
)
def test_ac_1_known_secrets_are_env_ref_fields(path):
    spec = ss.field_by_path(path)
    assert spec is not None and spec.secret and spec.kind is ss.Kind.SECRET_REF, (
        f"{path} must be an env-var-reference secret field (ADR-0013)"
    )


@pytest.mark.parametrize(
    "path",
    [
        "controllers[].host",
        "home_assistant.url",
        "dns_sources[].instances[].url",
        "reboot.enabled",
        "reboot.proactive.enabled",
    ],
)
def test_ac_1_startup_only_fields_are_restart_required(path):
    spec = ss.field_by_path(path)
    assert spec is not None and spec.restart_required, (
        f"{path} is consumed only at startup and must be marked restart_required"
    )
