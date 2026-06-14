from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .resolution import apply_quiet_hours, quiet_hours_active, resolve_thresholds


def is_bad_state(samples: list[Any], thresholds: dict[str, Any], radios: tuple[str, ...]) -> bool:
    if not samples:
        return False
    # ADR-0009: each client criterion is disable-able — a `None` threshold means
    # "don't test this signal". Read once; a None value skips that condition below.
    signal_max = thresholds.get("signal_dbm_max")
    tx_rate_max = thresholds.get("tx_rate_kbps_max")
    retry_max = thresholds.get("retry_pct_max")
    # Fail safe: with no active client criterion, "bad" would be vacuously true for
    # every client on a saturated AP — never act on radio + saturation alone.
    # load_config already rejects an all-null trio (config.py); this guards direct
    # callers (tests, embedders) too.
    if signal_max is None and tx_rate_max is None and retry_max is None:
        return False
    for sample in samples:
        if sample.radio not in radios:
            return False
        # ADR-0008 AP-saturation gate: only act on a saturated AP (PLAN.md §3).
        # .get(..., 0) keeps it off when ap_cu_total_min is absent/0, so a 3-key
        # thresholds dict and the shipped default behave as before. UniFi writes 0
        # when CU is unavailable, so unknown CU fails closed (0 < any positive floor).
        if (sample.ap_cu_total or 0) < thresholds.get("ap_cu_total_min", 0):
            return False
        if signal_max is not None and sample.signal >= signal_max:
            return False
        if tx_rate_max is not None and sample.tx_rate_kbps >= tx_rate_max:
            return False
        if retry_max is not None:
            attempts = sample.wifi_tx_attempts or 0
            if attempts <= 0:
                return False
            retry_pct = (sample.tx_retries * 100.0) / attempts
            if retry_pct <= retry_max:
                return False
    return True


class Scorer:
    def __init__(self, config: Any, *, wall_now_fn: Callable[[], float] = time.time) -> None:
        self.config = config
        self._windows: dict[str, deque] = {}
        # Injected wall clock for quiet-hours evaluation (tests pass a lambda /
        # time-machine); production uses time.time. Monotonic is unusable here —
        # quiet hours is a wall-clock-of-day concept (ADR-0007).
        self.wall_now_fn = wall_now_fn

    def _window(self, mac: str) -> deque:
        if mac not in self._windows:
            self._windows[mac] = deque(maxlen=self.config.scanner.window_samples)
        return self._windows[mac]

    def ingest(self, client: Any) -> dict[str, Any] | None:
        mac = client.mac
        if mac in self.config.allowlist:
            return None
        thresholds = resolve_thresholds(mac, self.config)
        # ADR-0007: during quiet hours, tighten to the stricter override thresholds
        # (per-field more-conservative-wins) before testing bad-state.
        quiet_hours = self.config.quiet_hours
        if quiet_hours is not None and quiet_hours_active(self.wall_now_fn(), quiet_hours):
            thresholds = apply_quiet_hours(thresholds, quiet_hours)
        radios = self.config.detection.radios
        window = self._window(mac)
        window.append(client)
        if len(window) < self.config.scanner.window_samples:
            return None
        if is_bad_state(list(window), thresholds, radios):
            return thresholds
        return None
