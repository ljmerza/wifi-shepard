from __future__ import annotations

from collections import deque
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


def is_bad_state(samples: list[Any], thresholds: dict[str, Any], radios: tuple[str, ...]) -> bool:
    if not samples:
        return False
    for sample in samples:
        if sample.radio not in radios:
            return False
        if sample.signal >= thresholds["signal_dbm_max"]:
            return False
        if sample.tx_rate_kbps >= thresholds["tx_rate_kbps_max"]:
            return False
        attempts = sample.wifi_tx_attempts or 0
        if attempts <= 0:
            return False
        retry_pct = (sample.tx_retries * 100.0) / attempts
        if retry_pct <= thresholds["retry_pct_max"]:
            return False
    return True


class Scorer:
    def __init__(self, config: Any) -> None:
        self.config = config
        self._windows: dict[str, deque] = {}

    def _window(self, mac: str) -> deque:
        if mac not in self._windows:
            self._windows[mac] = deque(maxlen=self.config.scanner.window_samples)
        return self._windows[mac]

    def ingest(self, client: Any) -> dict[str, Any] | None:
        mac = client.mac
        if mac in self.config.allowlist:
            return None
        thresholds = resolve_thresholds(mac, self.config)
        radios = self.config.detection.radios
        window = self._window(mac)
        window.append(client)
        if len(window) < self.config.scanner.window_samples:
            return None
        if is_bad_state(list(window), thresholds, radios):
            return thresholds
        return None
