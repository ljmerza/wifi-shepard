"""ADR-0010: traffic-inactivity detection — an "associated but no traffic" flatline
detector, independent of the conjunctive signal/rate/retry scorer.

The conjunctive ``Scorer`` (``scorer.is_bad_state``) requires weak signal AND low
rate AND high retry simultaneously, so a strong-signal client whose *application*
session has wedged (pristine WiFi telemetry, dead cloud MQTT) can never qualify.
This detector fills that blind spot: for an explicitly opted-in MAC, it watches the
client's cumulative byte counters and flags it when they flatline (near-zero summed
delta) across a full window.

Design notes (see ADR-0010):

- **Opt-in only.** Only MACs in ``detection.inactivity.macs`` are evaluated; every
  other client is ignored (and never tracked, so memory stays bounded). No baseline
  learning in v1 — "this device normally holds a WAN session" is the operator's
  assertion, not something inferred.
- **Independent of the scorer.** Signal / tx_rate / retry thresholds, the
  ``ap_cu_total_min`` AP-saturation gate, and quiet-hours tightening deliberately do
  NOT apply here — those gates are exactly what hid this failure mode.
- **Fail-safe.** ``None`` byte counters (backend didn't report them) can't be
  evaluated, so they clear the window and never flag. A negative delta (a counter
  reset from the client reassociating / rebooting) counts as activity and clears the
  window. Downstream, the actor's backoff / caps / rate-limits / dry-run gate apply
  unchanged because a flag is dispatched through ``Actor.handle`` like any other kick.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from .reboot import normalize_mac


class InactivityScorer:
    """Per-MAC byte-counter flatline detector (ADR-0010), in the mold of ``Scorer``.

    Holds, per opted-in MAC, the previous cumulative ``tx_bytes + rx_bytes`` total and
    a bounded deque of successive deltas (``maxlen = window_samples``). ``ingest`` is
    called once per polled client; it returns a decision dict when the MAC flatlines,
    else ``None``.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        # Previous cumulative (tx+rx) total per MAC; absent until the first usable
        # sample. Deltas are diffed against this.
        self._prev: dict[str, int] = {}
        # Sliding window of successive byte deltas per MAC.
        self._windows: dict[str, deque] = {}

    def _window(self, mac: str) -> deque:
        if mac not in self._windows:
            # maxlen is baked in at first sight of the MAC; a window_samples change
            # rebuilds the whole scorer (pipeline.update_config), mirroring Scorer.
            self._windows[mac] = deque(maxlen=self.config.detection.inactivity.window_samples)
        return self._windows[mac]

    def _clear(self, mac: str) -> None:
        """Reset a MAC's accumulated state so it re-accumulates from scratch."""
        self._prev.pop(mac, None)
        self._windows.pop(mac, None)

    def ingest(self, client: Any) -> dict[str, Any] | None:
        cfg = self.config.detection.inactivity
        # Master switch off → the whole class is inert.
        if not cfg.enabled:
            return None
        mac = normalize_mac(client.mac)
        # Defense in depth: an allowlisted MAC is never actioned, even if the operator
        # also opted it into inactivity (config load already warns on this overlap).
        if mac in self.config.allowlist:
            return None
        # Opt-in gate: only evaluate (and only track) explicitly listed MACs.
        if mac not in cfg.macs:
            return None

        tx = getattr(client, "tx_bytes", None)
        rx = getattr(client, "rx_bytes", None)
        # Fail-safe: without both counters we can't compute a delta. Clear so a
        # resumed counter starts a fresh window rather than diffing across a gap.
        if tx is None or rx is None:
            self._clear(mac)
            return None

        total = tx + rx
        prev = self._prev.get(mac)
        self._prev[mac] = total
        if prev is None:
            # First usable sample: establish the baseline, no delta yet.
            return None

        delta = total - prev
        if delta < 0:
            # Counter reset: the client reassociated / rebooted — that IS activity.
            # Drop the stale window; the new baseline is already recorded above.
            self._windows.pop(mac, None)
            return None

        window = self._window(mac)
        window.append(delta)
        if len(window) < window.maxlen:
            return None

        total_bytes = sum(window)
        if total_bytes < cfg.min_bytes_per_window:
            # Flatlined across a full window. Clear the window so the MAC re-accumulates
            # rather than re-flagging every poll — backoff / caps / rate-limits are the
            # additional downstream protection (all applied via Actor.handle).
            self._windows.pop(mac, None)
            return {
                "trigger": "inactivity",
                "window_bytes": total_bytes,
                "min_bytes_per_window": cfg.min_bytes_per_window,
                "window_samples": window.maxlen,
            }
        return None
