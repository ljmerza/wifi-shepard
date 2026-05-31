from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    NORMAL = "NORMAL"
    QUARANTINE = "QUARANTINE"


@dataclass
class MacState:
    kick_count: int = 0
    quarantine_notified: bool = False


class BackoffManager:
    def __init__(self, *, quarantine_after_kicks: int = 5) -> None:
        self._states: dict[str, MacState] = {}
        self.quarantine_after_kicks = quarantine_after_kicks

    def _entry(self, mac: str) -> MacState:
        if mac not in self._states:
            self._states[mac] = MacState()
        return self._states[mac]

    def kick_count(self, mac: str) -> int:
        return self._entry(mac).kick_count

    def state(self, mac: str) -> State:
        if self._entry(mac).kick_count >= self.quarantine_after_kicks:
            return State.QUARANTINE
        return State.NORMAL

    def record_kick(self, mac: str) -> None:
        self._entry(mac).kick_count += 1

    def should_quarantine(self, mac: str) -> bool:
        return self._entry(mac).kick_count >= self.quarantine_after_kicks

    def quarantine_notified(self, mac: str) -> bool:
        return self._entry(mac).quarantine_notified

    def mark_quarantine_notified(self, mac: str) -> None:
        self._entry(mac).quarantine_notified = True


_HOUR_SECONDS = 3600.0
_DAY_SECONDS = 86400.0


def evaluate_backoff(
    recent_ts: list[float],
    now: float,
    *,
    cooldowns: tuple[int, ...],
    max_per_hour: int,
    max_per_day: int,
) -> tuple[bool, str | None, float | None]:
    """Per-MAC action-policy gate (ADR-0007): escalating cooldown + hourly/daily caps.

    Pure. ``recent_ts`` is this MAC's real-kick timestamps (ascending, wall-clock
    seconds) over at least the last ``max(_DAY_SECONDS, max(cooldowns))``; the caller
    reads them from ``kick_events`` so the decision survives a restart. Returns
    ``(allowed, reason, retry_after_seconds)`` mirroring ``rate_limit.py``.

    Order: daily cap → hourly cap → cooldown; the first gate to trip wins, with a
    ``retry_after`` of when it next clears. Each limit is opt-in (0 / empty = off).
    """
    if max_per_day > 0:
        day = [t for t in recent_ts if t >= now - _DAY_SECONDS]
        if len(day) >= max_per_day:
            return False, "per_mac_daily_cap", _DAY_SECONDS - (now - min(day))
    if max_per_hour > 0:
        hour = [t for t in recent_ts if t >= now - _HOUR_SECONDS]
        if len(hour) >= max_per_hour:
            return False, "per_mac_hourly_cap", _HOUR_SECONDS - (now - min(hour))
    if cooldowns:
        # Escalation level = the trailing run of kicks within the recovery window
        # (= the longest cooldown). After that much quiet the run empties, so the
        # next kick starts again at the first rung.
        window = float(max(cooldowns))
        run = [t for t in recent_ts if t >= now - window]
        if run:
            idx = min(len(run) - 1, len(cooldowns) - 1)
            required = float(cooldowns[idx])
            elapsed = now - max(run)
            if elapsed < required:
                return False, "per_mac_cooldown", required - elapsed
    return True, None, None
