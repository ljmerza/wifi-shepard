"""Per-MAC config resolution: override > global default.

These helpers resolve effective per-client settings from the global
``detection:``/``scanner:`` blocks plus any matching ``overrides:`` entry. They
are a *config* concern, not a *scoring* concern — kept out of ``scorer.py`` so the
actor can resolve a kick mechanism without importing the scorer (ADR threshold
resolution semantics; mirrors ADR-0001 AC-6 / ADR-0003 AC-5).
"""

from __future__ import annotations

from typing import Any

_THRESHOLD_FIELDS = ("tx_rate_kbps_max", "retry_pct_max", "signal_dbm_max")


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
