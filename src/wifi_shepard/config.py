from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DetectionConfig:
    tx_rate_kbps_max: int
    retry_pct_max: int
    signal_dbm_max: int


@dataclass(frozen=True)
class OverrideEntry:
    mac: str
    tx_rate_kbps_max: int | None = None
    retry_pct_max: int | None = None
    signal_dbm_max: int | None = None


@dataclass(frozen=True)
class Config:
    detection: DetectionConfig
    overrides: tuple[OverrideEntry, ...] = ()


def build_config(
    *,
    tx_rate_kbps_max: int,
    retry_pct_max: int,
    signal_dbm_max: int,
    overrides: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> Config:
    detection = DetectionConfig(
        tx_rate_kbps_max=tx_rate_kbps_max,
        retry_pct_max=retry_pct_max,
        signal_dbm_max=signal_dbm_max,
    )
    overrides_typed = tuple(OverrideEntry(**o) for o in overrides)
    return Config(detection=detection, overrides=overrides_typed)
