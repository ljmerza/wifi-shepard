"""In-flight kick bookkeeping for the Actor.

Extracted from Actor so ``handle()`` reads as kick *policy* and delegates the
two state maps' mechanics here (SOLID review M1). Values are kept as plain
dicts so the actor's hot-path reads (``pending["group"]``, ``pending["ap_id"]``)
are unchanged — this is a pure relocation of state, not a behavior change.

Two independent maps, both keyed by MAC:

- ``btm``: a BTM request sent on a prior cycle, awaiting either a roam (success)
  or a still-bad-state on the same AP next cycle (deauth fallback under the same
  attempt_group). Value: ``{"group", "ap_id"}``.
- ``outcome``: the AP + mechanism captured at kick time, consumed on the next
  cycle to log ``kick_succeeded`` / ``kick_no_roam``. Value:
  ``{"ap_id", "mechanism", "attempt_group"}``.
"""

from __future__ import annotations


class PendingKicks:
    def __init__(self) -> None:
        self._btm: dict[str, dict[str, str]] = {}
        self._outcome: dict[str, dict[str, str]] = {}

    # --- BTM -> deauth fallback (ADR-0003 AC-4) ---
    def set_btm(self, mac: str, *, group: str, ap_id: str) -> None:
        self._btm[mac] = {"group": group, "ap_id": ap_id}

    def get_btm(self, mac: str) -> dict[str, str] | None:
        return self._btm.get(mac)

    def clear_btm(self, mac: str) -> None:
        self._btm.pop(mac, None)

    def has_btm(self, mac: str) -> bool:
        return mac in self._btm

    # --- post-kick roam check (ADR-0003 AC-6) ---
    def set_outcome(self, mac: str, *, ap_id: str, mechanism: str, attempt_group: str) -> None:
        self._outcome[mac] = {
            "ap_id": ap_id,
            "mechanism": mechanism,
            "attempt_group": attempt_group,
        }

    def pop_outcome(self, mac: str) -> dict[str, str] | None:
        return self._outcome.pop(mac, None)
