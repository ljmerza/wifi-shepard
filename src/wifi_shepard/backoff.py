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
