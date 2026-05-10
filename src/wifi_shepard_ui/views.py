"""Read-model for the UI sidecar.

Every SQL query lives here. The daemon's tables (`client_samples`, `kick_events`)
are the only contract this module knows about. UI routes call these functions
and render the dataclasses they return — no SQL leaks into `app.py`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Cooldown schedule from PLAN.md §4. Indexed by 1-based kick count, capped at
# the last bucket. Mirrored here (not imported from the daemon) on purpose:
# the UI is a read-side, decoupled from the daemon's Python tree.
COOLDOWN_SECONDS: tuple[int, ...] = (300, 1800, 7200, 43200, 86400)
EVALUATING_WINDOW_SECONDS: int = 1800
QUARANTINE_AT_KICKS: int = 5


@dataclass(frozen=True)
class DeviceRow:
    mac: str
    kick_count: int
    last_kick_ts: float | None  # newest kick; None if never kicked
    last_event_ts: float | None  # newest of (last kick, last sample)
    state: str
    allowlisted: bool


@dataclass(frozen=True)
class HistoryEvent:
    ts: float
    kind: str  # "kick", "kick_dry_run", or "sample"
    detail: str  # human-readable line of context
    mechanism: str | None = None  # 'deauth' / 'btm' / 'deauth_fallback' for kick rows
    attempt_group: str | None = None  # UUID linking BTM+deauth_fallback pairs (ADR-0003 AC-7)


@dataclass(frozen=True)
class OverviewStats:
    total_clients: int
    quarantined: int
    kicks_today: int
    kicks_this_week: int
    noisy_aps: list[tuple[str, int]]  # (ap_id, latest_cu_total), top 5


def derive_state(*, kick_count: int, last_kick_ts: float | None, now: float) -> str:
    """Map kick history to a backoff-state label.

    Mirrors the v1 cooldown schedule from PLAN.md §4. Returns one of
    NORMAL / KICKED / EVALUATING / QUARANTINE. KICK_PENDING is a transient
    in-memory state inside the daemon's scorer — it is never persisted, so
    the UI cannot observe it.

    QUARANTINE is sticky by design: once a MAC crosses
    QUARANTINE_AT_KICKS, the daemon stops kicking it forever (PLAN.md §4
    "QUARANTINE: notify, no more kicks"). Operators clear quarantine by
    editing the daemon's config or wiping kick_events, not via decay.
    """
    if kick_count >= QUARANTINE_AT_KICKS:
        return "QUARANTINE"
    if kick_count == 0 or last_kick_ts is None:
        return "NORMAL"
    bucket = min(kick_count, len(COOLDOWN_SECONDS)) - 1
    cooldown = COOLDOWN_SECONDS[bucket]
    elapsed = now - last_kick_ts
    if elapsed < cooldown:
        return "KICKED"
    if elapsed < cooldown + EVALUATING_WINDOW_SECONDS:
        return "EVALUATING"
    return "NORMAL"


def list_devices(
    conn: sqlite3.Connection,
    *,
    allowlist: set[str],
    now: float,
) -> list[DeviceRow]:
    """One row per MAC seen in either table, with derived state + kick count.

    Counts and ts come from REAL kicks only (`dry_run=0`); dry-run rows are
    "would-kicks" the daemon never actually sent. Counting them here would
    let a never-actually-kicked MAC reach QUARANTINE, contradicting
    overview() which already filters dry_run=0.
    """
    kicks: dict[str, tuple[int, float]] = {}
    for mac, n_kicks, last_kick_ts in conn.execute(
        "SELECT mac, COUNT(*), MAX(ts) FROM kick_events WHERE dry_run = 0 GROUP BY mac"
    ):
        kicks[mac] = (n_kicks, last_kick_ts)

    samples: dict[str, float] = {}
    for mac, last_sample_ts in conn.execute("SELECT mac, MAX(ts) FROM client_samples GROUP BY mac"):
        samples[mac] = last_sample_ts

    allowlist_norm = {m.lower() for m in allowlist}
    rows: list[DeviceRow] = []
    for mac in sorted(set(kicks) | set(samples)):
        n_kicks, last_kick_ts = kicks.get(mac, (0, None))
        last_sample_ts = samples.get(mac)
        last_event_ts = max(
            (ts for ts in (last_kick_ts, last_sample_ts) if ts is not None),
            default=None,
        )
        rows.append(
            DeviceRow(
                mac=mac,
                kick_count=n_kicks,
                last_kick_ts=last_kick_ts,
                last_event_ts=last_event_ts,
                state=derive_state(kick_count=n_kicks, last_kick_ts=last_kick_ts, now=now),
                allowlisted=mac.lower() in allowlist_norm,
            )
        )
    return rows


def sort_devices(rows: list[DeviceRow], key: str) -> list[DeviceRow]:
    """Stable sort for a small set of well-known columns.

    'last_bad' sorts by last_kick_ts (the actual bad-window anchor), not by
    last_event_ts — otherwise a healthy MAC with frequent samples floats to
    the top and drowns out actually-noisy devices.
    """
    sorters = {
        "mac": (lambda r: r.mac, False),
        "kicks": (lambda r: r.kick_count, True),
        "last_bad": (lambda r: r.last_kick_ts or 0, True),
        "state": (lambda r: r.state, False),
    }
    keyfn, reverse = sorters.get(key, sorters["mac"])
    return sorted(rows, key=keyfn, reverse=reverse)


def device_history(
    conn: sqlite3.Connection,
    *,
    mac: str,
    limit: int = 200,
) -> list[HistoryEvent]:
    """Chronological-newest-first merge of client_samples + kick_events for one MAC.

    MAC matching is case-insensitive: aiounifi and other backends normalize MAC
    case inconsistently across firmware versions, and an operator hand-typing
    a URL shouldn't get a silently-empty timeline.
    """
    events: list[HistoryEvent] = []
    for ts, dry_run, mechanism, attempt_group in conn.execute(
        "SELECT ts, dry_run, mechanism, attempt_group FROM kick_events "
        "WHERE mac = ? COLLATE NOCASE ORDER BY ts DESC LIMIT ?",
        (mac, limit),
    ):
        if dry_run:
            events.append(
                HistoryEvent(
                    ts=ts,
                    kind="kick_dry_run",
                    detail="would-kick (dry-run)",
                    mechanism=mechanism,
                    attempt_group=attempt_group,
                )
            )
        else:
            events.append(
                HistoryEvent(
                    ts=ts,
                    kind="kick",
                    detail="kick",
                    mechanism=mechanism,
                    attempt_group=attempt_group,
                )
            )

    for ts, signal, tx_rate, retries, attempts, radio, ap, cu in conn.execute(
        "SELECT ts, signal, tx_rate_kbps, tx_retries, wifi_tx_attempts, "
        "       radio, ap_id, ap_cu_total "
        "FROM client_samples WHERE mac = ? COLLATE NOCASE "
        "ORDER BY ts DESC LIMIT ?",
        (mac, limit),
    ):
        retry_pct = (retries / attempts * 100) if attempts else 0
        events.append(
            HistoryEvent(
                ts=ts,
                kind="sample",
                detail=(
                    f"signal={signal}dBm tx_rate={tx_rate}kbps retries={retry_pct:.0f}% "
                    f"radio={radio} ap={ap} cu={cu}%"
                ),
            )
        )

    events.sort(key=lambda e: e.ts, reverse=True)
    return events


def overview(conn: sqlite3.Connection, *, now: float) -> OverviewStats:
    """Snapshot for the GET / page."""
    day_ago = now - 86400
    week_ago = now - 86400 * 7

    (total_clients,) = conn.execute("SELECT COUNT(DISTINCT mac) FROM client_samples").fetchone()

    (kicks_today,) = conn.execute(
        "SELECT COUNT(*) FROM kick_events WHERE dry_run = 0 AND ts > ?",
        (day_ago,),
    ).fetchone()
    (kicks_this_week,) = conn.execute(
        "SELECT COUNT(*) FROM kick_events WHERE dry_run = 0 AND ts > ?",
        (week_ago,),
    ).fetchone()

    quarantined = 0
    for _mac, n_kicks, last_kick_ts in conn.execute(
        "SELECT mac, COUNT(*), MAX(ts) FROM kick_events WHERE dry_run = 0 GROUP BY mac"
    ):
        if derive_state(kick_count=n_kicks, last_kick_ts=last_kick_ts, now=now) == "QUARANTINE":
            quarantined += 1

    # Filter NULL ap_id INSIDE the GROUP BY subquery — otherwise NULL becomes
    # its own group, takes a slot in the LIMIT 5, and is dropped after the
    # fact, causing fewer than 5 real APs to surface even when 5 were
    # available.
    noisy_aps: list[tuple[str, int]] = []
    for ap_id, cu in conn.execute(
        "SELECT ap_id, ap_cu_total FROM client_samples "
        "WHERE id IN ("
        "  SELECT MAX(id) FROM client_samples "
        "  WHERE ap_id IS NOT NULL AND ap_cu_total IS NOT NULL "
        "  GROUP BY ap_id"
        ") "
        "ORDER BY ap_cu_total DESC LIMIT 5"
    ):
        noisy_aps.append((ap_id, cu))

    return OverviewStats(
        total_clients=total_clients,
        quarantined=quarantined,
        kicks_today=kicks_today,
        kicks_this_week=kicks_this_week,
        noisy_aps=noisy_aps,
    )
