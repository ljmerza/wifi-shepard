"""Per-MAC config resolution: override > global default.

These helpers resolve effective per-client settings from the global
``detection:``/``scanner:`` blocks plus any matching ``overrides:`` entry, and the
quiet-hours tightening (ADR-0007). They are a *config* concern, not a *scoring*
concern — kept out of ``scorer.py`` so the actor can resolve a kick mechanism or
caps without importing the scorer (threshold-resolution semantics; mirrors
ADR-0001 AC-6 / ADR-0003 AC-5 / ADR-0007).
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

_THRESHOLD_FIELDS = ("tx_rate_kbps_max", "retry_pct_max", "signal_dbm_max", "ap_cu_total_min")


def resolve_thresholds(mac: str, config: Any) -> dict[str, Any]:
    resolved = {name: getattr(config.detection, name) for name in _THRESHOLD_FIELDS}
    for override in config.overrides:
        if override.mac != mac:
            continue
        for name in _THRESHOLD_FIELDS:
            value = getattr(override, name, None)
            if value is not None:
                resolved[name] = value
        break
    return resolved


def resolve_kick_mechanism(mac: str, config: Any) -> str:
    """Per-MAC override > global default. ADR-0003 AC-5 (mirrors ADR-0001 AC-6)."""
    for override in config.overrides:
        if override.mac == mac and getattr(override, "kick_mechanism", None) is not None:
            return override.kick_mechanism
    return config.scanner.kick_mechanism


def resolve_caps(mac: str, config: Any) -> tuple[int, int]:
    """Per-MAC kick-cap resolution (ADR-0007): override > global, per field.

    Returns ``(max_kicks_per_hour, max_kicks_per_day)``; 0 means that cap is off.
    """
    max_hour = config.backoff.max_kicks_per_hour
    max_day = config.backoff.max_kicks_per_day
    for override in config.overrides:
        if override.mac != mac:
            continue
        if getattr(override, "max_kicks_per_hour", None) is not None:
            max_hour = override.max_kicks_per_hour
        if getattr(override, "max_kicks_per_day", None) is not None:
            max_day = override.max_kicks_per_day
        break
    return max_hour, max_day


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = value.split(":")
    return dt_time(int(hour), int(minute))


def quiet_hours_active(now_epoch: float, quiet_hours: Any) -> bool:
    """True when ``now_epoch`` (wall-clock seconds) falls in the quiet window.

    The window is [start, end) in ``quiet_hours.timezone`` and may wrap midnight
    (e.g. 23:00–07:00). Config validation already guaranteed HH:MM + a valid zone.
    """
    tz = ZoneInfo(quiet_hours.timezone)
    now_local = datetime.fromtimestamp(now_epoch, tz).time()
    start = _parse_hhmm(quiet_hours.start)
    end = _parse_hhmm(quiet_hours.end)
    if start <= end:
        return start <= now_local < end
    # Window wraps midnight: active from start to 24:00 and from 00:00 to end.
    return now_local >= start or now_local < end


def apply_quiet_hours(thresholds: dict[str, Any], quiet_hours: Any) -> dict[str, Any]:
    """Tighten ``thresholds`` to the stricter quiet-hours overrides, per field.

    'Stricter' = more conservative (kicks fewer): tx_rate_kbps_max lower, retry_pct_max
    higher, signal_dbm_max lower (more negative). Quiet hours never *loosens* a value;
    fields the operator left unset in override_threshold keep their resolved value.
    """
    out = dict(thresholds)
    if quiet_hours.tx_rate_kbps_max is not None:
        out["tx_rate_kbps_max"] = min(out["tx_rate_kbps_max"], quiet_hours.tx_rate_kbps_max)
    if quiet_hours.retry_pct_max is not None:
        out["retry_pct_max"] = max(out["retry_pct_max"], quiet_hours.retry_pct_max)
    if quiet_hours.signal_dbm_max is not None:
        out["signal_dbm_max"] = min(out["signal_dbm_max"], quiet_hours.signal_dbm_max)
    return out
