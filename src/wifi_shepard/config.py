from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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


@dataclass(frozen=True)
class BackoffConfig:
    quarantine_after_kicks: int = 5


@dataclass(frozen=True)
class OverrideEntry:
    mac: str
    tx_rate_kbps_max: int | None = None
    retry_pct_max: int | None = None
    signal_dbm_max: int | None = None


@dataclass(frozen=True)
class Config:
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    backoff: BackoffConfig = field(default_factory=BackoffConfig)
    overrides: tuple[OverrideEntry, ...] = ()
    allowlist: tuple[str, ...] = ()


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
    overrides: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    allowlist: list[str] | tuple[str, ...] = (),
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
    )
    backoff = BackoffConfig(quarantine_after_kicks=quarantine_after_kicks)
    known = {f.name for f in dataclasses.fields(OverrideEntry)}
    overrides_typed = tuple(
        OverrideEntry(**{k: v for k, v in o.items() if k in known}) for o in overrides
    )
    return Config(
        detection=detection,
        scanner=scanner,
        backoff=backoff,
        overrides=overrides_typed,
        allowlist=tuple(allowlist),
    )


def load_config_from_path(path: Path | str) -> Config:
    text = Path(path).read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a YAML mapping, got {type(data).__name__}")

    scanner_data = data.get("scanner") or {}
    detection_data = data.get("detection") or {}
    backoff_data = data.get("backoff") or {}

    raw_dry_run = scanner_data.get("dry_run", True)
    dry_run = True if raw_dry_run is None else bool(raw_dry_run)

    return build_config(
        poll_interval_seconds=int(scanner_data.get("poll_interval_seconds", 60)),
        window_samples=int(scanner_data.get("window_samples", 5)),
        dry_run=dry_run,
        tx_rate_kbps_max=int(detection_data.get("tx_rate_kbps_max", 12000)),
        retry_pct_max=int(detection_data.get("retry_pct_max", 30)),
        signal_dbm_max=int(detection_data.get("signal_dbm_max", -70)),
        radios=tuple(detection_data.get("radios") or ("ng",)),
        quarantine_after_kicks=int(backoff_data.get("quarantine_after_kicks", 5)),
        allowlist=tuple(data.get("allowlist") or ()),
        overrides=tuple(data.get("overrides") or ()),
    )
