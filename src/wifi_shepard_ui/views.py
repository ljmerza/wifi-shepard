"""Read-model for the UI sidecar.

Every SQL query lives here. The daemon's tables (`client_samples`, `kick_events`)
are the only contract this module knows about. UI routes call these functions
and render the dataclasses they return — no SQL leaks into `app.py`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# ADR-0002 §Risks + ADR-0003 Phase 6: every column views.py reads from
# kick_events is listed here so the sidecar can fail fast at startup if the
# daemon's migration hasn't run yet. Without this, a partial-deploy window
# (new sidecar image, daemon still on the old schema) surfaces as opaque
# 500s on /devices/{mac} instead of a clear "schema mismatch" log line.
_REQUIRED_KICK_EVENTS_COLUMNS: frozenset[str] = frozenset(
    {"ts", "mac", "dry_run", "mechanism", "attempt_group"}
)


class SchemaMismatch(RuntimeError):
    """The daemon's kick_events table is missing one or more columns the UI reads."""


def assert_kick_events_schema(conn: sqlite3.Connection) -> None:
    """Raise SchemaMismatch if kick_events EXISTS but lacks any column views.py reads.

    If the table doesn't exist at all, this is the empty-state case (daemon
    mid-startup, schema not yet created) and the request-path's _safe_read
    will render the empty-state page — return silently here.
    """
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kick_events'")
    if cur.fetchone() is None:
        return
    cur = conn.execute("PRAGMA table_info(kick_events)")
    present = {row[1] for row in cur.fetchall()}
    missing = _REQUIRED_KICK_EVENTS_COLUMNS - present
    if missing:
        raise SchemaMismatch(
            f"kick_events is missing required columns {sorted(missing)}; "
            f"the daemon's ADR-0003 migration probably hasn't run against this DB yet"
        )


# Cooldown schedule from PLAN.md §4. Indexed by 1-based kick count, capped at
# the last bucket. Mirrored here (not imported from the daemon) on purpose:
# the UI is a read-side, decoupled from the daemon's Python tree.
COOLDOWN_SECONDS: tuple[int, ...] = (300, 1800, 7200, 43200, 86400)
EVALUATING_WINDOW_SECONDS: int = 1800
QUARANTINE_AT_KICKS: int = 5

# Sparkline series lengths. Both are point counts, not durations: the AP trend
# plots the last N polls (whatever the daemon's scan_interval happens to be),
# while the kick trend is bucketed into fixed one-hour slots.
TREND_POINTS: int = 24
KICK_TREND_HOURS: int = 24


@dataclass(frozen=True)
class DeviceRow:
    mac: str
    kick_count: int
    last_kick_ts: float | None  # newest kick; None if never kicked
    last_event_ts: float | None  # newest of (last kick, last sample)
    state: str
    allowlisted: bool
    name: str | None = None  # latest controller-reported name/hostname, if any


@dataclass(frozen=True)
class HistoryEvent:
    ts: float
    kind: str  # "kick", "kick_dry_run", or "sample"
    detail: str  # human-readable line of context
    mechanism: str | None = None  # 'deauth' / 'btm' / 'deauth_fallback' for kick rows
    attempt_group: str | None = None  # UUID linking BTM+deauth_fallback pairs (ADR-0003 AC-7)


@dataclass(frozen=True)
class RadioUtil:
    radio: str  # backend radio id: "ng" (2.4), "na" (5), "6e"/"ax" (6)
    channel: int
    cu_total: int


@dataclass(frozen=True)
class NoisyAP:
    name: str | None
    mac: str
    cpu_pct: float | None
    mem_pct: float | None
    radios: list[RadioUtil]  # noisiest radio first
    cu_trend: tuple[int, ...] = ()  # recent cu_total for the noisiest radio, oldest-first


@dataclass(frozen=True)
class OverviewStats:
    total_clients: int
    quarantined: int
    kicks_today: int
    kicks_this_week: int
    noisy_aps: list[NoisyAP]  # top 5 APs by peak per-radio channel utilization
    kicks_by_hour: tuple[int, ...] = ()  # real kicks per hour, trailing 24h, oldest-first


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

    # Latest controller-reported name per MAC. Cosmetic, so degrade silently if
    # the daemon hasn't run the `name`-column migration yet (partial deploy):
    # an absent column shows the MAC as before rather than 500-ing the page.
    names: dict[str, str | None] = {}
    try:
        for mac, name in conn.execute(
            "SELECT mac, name FROM client_samples "
            "WHERE id IN (SELECT MAX(id) FROM client_samples GROUP BY mac)"
        ):
            names[mac] = name
    except sqlite3.OperationalError:
        names = {}

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
                name=names.get(mac),
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
        "name": (lambda r: (r.name or "").lower(), False),
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


def _cu_trend(conn: sqlite3.Connection, *, ap_id: str, radio: str) -> tuple[int, ...]:
    """Recent channel-utilization samples for one AP radio, oldest-first.

    Feeds the overview sparkline. Returns () when the radio has no history, so
    the template renders a dash rather than a degenerate one-point <svg>.
    """
    try:
        rows = conn.execute(
            "SELECT cu_total FROM ap_radio_samples WHERE ap_id = ? AND radio = ? "
            "ORDER BY ts DESC LIMIT ?",
            (ap_id, radio, TREND_POINTS),
        ).fetchall()
    except sqlite3.OperationalError:
        return ()
    return tuple(int(cu or 0) for (cu,) in reversed(rows))


def kicks_by_hour(
    conn: sqlite3.Connection, *, now: float, hours: int = KICK_TREND_HOURS
) -> tuple[int, ...]:
    """Real kicks per hour over the trailing `hours`, oldest bucket first.

    Bucketing happens in Python rather than through SQLite's date functions so
    the read model stays agnostic about ts storage (the daemon writes an epoch
    float). Dry-run rows are excluded to match every other count on this page.
    """
    start = now - hours * 3600
    buckets = [0] * hours
    for (ts,) in conn.execute(
        "SELECT ts FROM kick_events WHERE dry_run = 0 AND ts > ?",
        (start,),
    ):
        # A kick landing exactly on `now` would index one past the last bucket;
        # clamp so it counts in the current hour instead of being dropped.
        idx = min(int((ts - start) // 3600), hours - 1)
        if idx >= 0:
            buckets[idx] += 1
    return tuple(buckets)


def _noisy_aps(conn: sqlite3.Connection, *, limit: int = 5) -> list[NoisyAP]:
    """Top APs by peak per-radio channel utilization, from the latest poll.

    Reads the newest ap_samples row per AP and the newest ap_radio_samples row
    per (AP, radio). Returns an empty list — rather than raising — when the AP
    tables don't exist yet (daemon not upgraded), so the overview tiles still
    render. Radios within each AP are ordered noisiest-first.
    """
    try:
        ap_rows = conn.execute(
            "SELECT ap_id, name, mac, cpu_pct, mem_pct FROM ap_samples "
            "WHERE id IN (SELECT MAX(id) FROM ap_samples GROUP BY ap_id)"
        ).fetchall()

        radios_by_ap: dict[str, list[RadioUtil]] = {}
        for ap_id, radio, channel, cu in conn.execute(
            "SELECT ap_id, radio, channel, cu_total FROM ap_radio_samples "
            "WHERE id IN (SELECT MAX(id) FROM ap_radio_samples GROUP BY ap_id, radio)"
        ):
            radios_by_ap.setdefault(ap_id, []).append(
                RadioUtil(radio=radio, channel=channel or 0, cu_total=cu or 0)
            )
    except sqlite3.OperationalError:
        return []

    ranked: list[tuple[int, NoisyAP]] = []
    for ap_id, name, mac, cpu_pct, mem_pct in ap_rows:
        radios = sorted(radios_by_ap.get(ap_id, []), key=lambda r: r.cu_total, reverse=True)
        peak = radios[0].cu_total if radios else 0
        # Trend the noisiest radio only — it's the one driving the AP's rank.
        trend = _cu_trend(conn, ap_id=ap_id, radio=radios[0].radio) if radios else ()
        ranked.append(
            (
                peak,
                NoisyAP(
                    name=name,
                    mac=mac,
                    cpu_pct=cpu_pct,
                    mem_pct=mem_pct,
                    radios=radios,
                    cu_trend=trend,
                ),
            )
        )
    ranked.sort(key=lambda t: t[0], reverse=True)
    return [ap for _peak, ap in ranked[:limit]]


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

    noisy_aps = _noisy_aps(conn)

    return OverviewStats(
        total_clients=total_clients,
        quarantined=quarantined,
        kicks_today=kicks_today,
        kicks_this_week=kicks_this_week,
        noisy_aps=noisy_aps,
        kicks_by_hour=kicks_by_hour(conn, now=now),
    )
