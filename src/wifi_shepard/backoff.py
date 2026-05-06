from __future__ import annotations


class BackoffManager:
    def __init__(self) -> None:
        self._kick_counts: dict[str, int] = {}

    def kick_count(self, mac: str) -> int:
        return self._kick_counts.get(mac, 0)

    def record_kick(self, mac: str) -> None:
        self._kick_counts[mac] = self.kick_count(mac) + 1
