from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _interpolate_env(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(
                f"env var ${{{name}}} referenced in config but not set in the environment"
            )
        return os.environ[name]

    return _ENV_VAR_PATTERN.sub(repl, text)


def _walk_and_interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return _interpolate_env(value)
    if isinstance(value, Mapping):
        return {k: _walk_and_interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk_and_interpolate(item) for item in value]
    return value


def _require_sequence(value: Any, key: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{key} must be a YAML list, got {type(value).__name__}: {value!r}")
    return list(value)


def _require_mapping_items(items: list[Any], key: str) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"{key}[{i}] must be a YAML mapping, got {type(item).__name__}: {item!r}"
            )
        out.append(item)
    return out


@dataclass(frozen=True)
class DetectionConfig:
    tx_rate_kbps_max: int = 12000
    retry_pct_max: int = 30
    signal_dbm_max: int = -70
    radios: tuple[str, ...] = ("ng",)


@dataclass(frozen=True)
class ScannerConfig:
    poll_interval_seconds: int = 60
    window_samples: int = 5
    dry_run: bool = True
    kick_mechanism: str = "deauth"


@dataclass(frozen=True)
class BackoffConfig:
    quarantine_after_kicks: int = 5


@dataclass(frozen=True)
class OverrideEntry:
    mac: str
    tx_rate_kbps_max: int | None = None
    retry_pct_max: int | None = None
    signal_dbm_max: int | None = None
    kick_mechanism: str | None = None


@dataclass(frozen=True)
class ControllerSpec:
    type: str
    name: str
    host: str
    username: str
    password: str
    site: str = "default"
    verify_ssl: bool = True


@dataclass(frozen=True)
class Config:
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    backoff: BackoffConfig = field(default_factory=BackoffConfig)
    overrides: tuple[OverrideEntry, ...] = ()
    allowlist: tuple[str, ...] = ()
    controllers: tuple[ControllerSpec, ...] = ()


_CONTROLLER_REQUIRED = ("type", "name", "host", "username", "password")


def _build_controller_spec(item: Mapping[str, Any], index: int) -> ControllerSpec:
    for key in _CONTROLLER_REQUIRED:
        if key not in item or item[key] in (None, ""):
            raise ValueError(f"controllers[{index}].{key} is required")
    return ControllerSpec(
        type=str(item["type"]),
        name=str(item["name"]),
        host=str(item["host"]),
        username=str(item["username"]),
        password=str(item["password"]),
        site=str(item.get("site", "default")),
        verify_ssl=bool(item.get("verify_ssl", True)),
    )


def build_config(
    *,
    tx_rate_kbps_max: int = 12000,
    retry_pct_max: int = 30,
    signal_dbm_max: int = -70,
    radios: tuple[str, ...] = ("ng",),
    dry_run: bool = True,
    window_samples: int = 5,
    poll_interval_seconds: int = 60,
    quarantine_after_kicks: int = 5,
    kick_mechanism: str = "deauth",
    overrides: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    allowlist: list[str] | tuple[str, ...] = (),
    controllers: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> Config:
    detection = DetectionConfig(
        tx_rate_kbps_max=tx_rate_kbps_max,
        retry_pct_max=retry_pct_max,
        signal_dbm_max=signal_dbm_max,
        radios=tuple(radios),
    )
    scanner = ScannerConfig(
        poll_interval_seconds=poll_interval_seconds,
        window_samples=window_samples,
        dry_run=dry_run,
        kick_mechanism=kick_mechanism,
    )
    backoff = BackoffConfig(quarantine_after_kicks=quarantine_after_kicks)
    known = {f.name for f in dataclasses.fields(OverrideEntry)}
    overrides_typed = tuple(
        OverrideEntry(**{k: v for k, v in o.items() if k in known}) for o in overrides
    )
    controllers_typed = tuple(_build_controller_spec(c, i) for i, c in enumerate(controllers))
    return Config(
        detection=detection,
        scanner=scanner,
        backoff=backoff,
        overrides=overrides_typed,
        allowlist=tuple(allowlist),
        controllers=controllers_typed,
    )


def load_config_from_path(path: Path | str) -> Config:
    text = Path(path).read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a YAML mapping, got {type(data).__name__}")

    data = _walk_and_interpolate(data)

    scanner_data = data.get("scanner") or {}
    detection_data = data.get("detection") or {}
    backoff_data = data.get("backoff") or {}

    raw_dry_run = scanner_data.get("dry_run", True)
    if raw_dry_run is None:
        dry_run = True
    elif isinstance(raw_dry_run, bool):
        dry_run = raw_dry_run
    else:
        raise ValueError(
            f"scanner.dry_run must be a boolean, got {type(raw_dry_run).__name__}: {raw_dry_run!r}"
        )

    radios_raw = detection_data.get("radios")
    radios_list = _require_sequence(radios_raw, "detection.radios")
    radios = tuple(radios_list) if radios_list else ("ng",)
    allowlist = tuple(_require_sequence(data.get("allowlist"), "allowlist"))
    overrides = tuple(
        _require_mapping_items(_require_sequence(data.get("overrides"), "overrides"), "overrides")
    )
    controllers = tuple(
        _require_mapping_items(
            _require_sequence(data.get("controllers"), "controllers"), "controllers"
        )
    )

    return build_config(
        poll_interval_seconds=int(scanner_data.get("poll_interval_seconds", 60)),
        window_samples=int(scanner_data.get("window_samples", 5)),
        dry_run=dry_run,
        kick_mechanism=str(scanner_data.get("kick_mechanism", "deauth")),
        tx_rate_kbps_max=int(detection_data.get("tx_rate_kbps_max", 12000)),
        retry_pct_max=int(detection_data.get("retry_pct_max", 30)),
        signal_dbm_max=int(detection_data.get("signal_dbm_max", -70)),
        radios=radios,
        quarantine_after_kicks=int(backoff_data.get("quarantine_after_kicks", 5)),
        allowlist=allowlist,
        overrides=overrides,
        controllers=controllers,
    )
