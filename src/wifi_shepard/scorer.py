from __future__ import annotations

from typing import Any

_THRESHOLD_FIELDS = ("tx_rate_kbps_max", "retry_pct_max", "signal_dbm_max")


def resolve_thresholds(mac: str, config: Any) -> dict[str, Any]:
    resolved = {field: getattr(config.detection, field) for field in _THRESHOLD_FIELDS}
    for override in config.overrides:
        if override.mac != mac:
            continue
        for field in _THRESHOLD_FIELDS:
            value = getattr(override, field, None)
            if value is not None:
                resolved[field] = value
        break
    return resolved
