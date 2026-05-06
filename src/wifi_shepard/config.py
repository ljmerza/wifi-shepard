from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
class OverrideEntry:
    mac: str
    tx_rate_kbps_max: int | None = None
    retry_pct_max: int | None = None
    signal_dbm_max: int | None = None


@dataclass(frozen=True)
class Config:
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
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
    overrides_typed = tuple(OverrideEntry(**o) for o in overrides)
    return Config(
        detection=detection,
        scanner=scanner,
        overrides=overrides_typed,
        allowlist=tuple(allowlist),
    )
