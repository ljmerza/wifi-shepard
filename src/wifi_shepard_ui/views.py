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


def assert_kick_events_schema(conn) -> None:
    """Raise SchemaMismatch if kick_events EXISTS but lacks any column views.py reads.

    If the table doesn't exist at all, this is the empty-state case (daemon
    mid-startup, schema not yet created) and the request-path's _safe_read
    will render the empty-state page — return silently here.

    Introspection is the one place SQL can't stay engine-agnostic: the MySQL
    adapter (WIFI_SHEPARD_DB_URL) exposes table_columns() instead of
    sqlite_master/PRAGMA, checking the same contract.
    """
    table_columns = getattr(conn, "table_columns", None)
    if table_columns is not None:
        present = table_columns("kick_events")
        if present is None:
            return
    else:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kick_events'"
        )
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
class DeviceSummary:
    """Header stats for the /devices/{mac} page."""

    mac: str
    name: str | None
    state: str
    allowlisted: bool
    kick_count: int  # real kicks only, matching list_devices / overview
    dry_run_count: int  # would-kicks the daemon never sent
    last_kick_ts: float | None  # newest real kick
    last_seen_ts: float | None  # newest client sample
    signal: int | None  # from the newest sample
    radio: str | None  # from the newest sample


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


@dataclass(frozen=True)
class DnsSourceHealth:
    """Latest per-poll heartbeat for one Pi-hole instance (ADR-0012)."""

    name: str
    ok: bool
    query_count: int
    last_ts: float | None
    error: str | None = None
    volume: tuple[int, ...] = ()  # recent query_counts, oldest-first (sparkline)


@dataclass(frozen=True)
class DnsObservation:
    """One near-threshold (MAC, domain) standing from the latest poll (ADR-0012)."""

    mac: str
    domain: str
    count: int
    threshold: int
    over_since: float | None
    name: str | None = None


@dataclass(frozen=True)
class DnsKick:
    """A kick that DNS-thrash detection triggered (ADR-0012)."""

    ts: float
    mac: str
    mechanism: str | None
    dry_run: bool
    name: str | None = None


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


# /devices filter vocabularies. Unknown values are ignored (treated as "no
# filter") rather than erroring: filters arrive as hand-editable URL params
# and a typo should render the unfiltered list, not a 500.
FILTER_STATES: frozenset[str] = frozenset({"NORMAL", "KICKED", "EVALUATING", "QUARANTINE"})
KICKED_WITHIN_SECONDS: dict[str, int] = {
    "24h": 86400,
    "7d": 86400 * 7,
    "30d": 86400 * 30,
}


def filter_devices(
    rows: list[DeviceRow],
    *,
    now: float,
    state: str = "",
    kicked_within: str = "",
    allowlist: str = "",
    q: str = "",
) -> list[DeviceRow]:
    """Apply the /devices URL-param filters; multiple filters AND together.

    - state: backoff-state label, case-insensitive ("kicked", "quarantine", …).
    - kicked_within: "24h" / "7d" / "30d" keep MACs with a real kick inside the
      window (matching the overview tiles' trailing windows); "never" keeps
      MACs with zero real kicks.
    - allowlist: "yes" / "no" on the allowlisted flag.
    - q: case-insensitive substring match on MAC or controller-reported name.
    """
    out = rows

    state_norm = state.strip().upper()
    if state_norm in FILTER_STATES:
        out = [r for r in out if r.state == state_norm]

    kicked_norm = kicked_within.strip().lower()
    if kicked_norm == "never":
        out = [r for r in out if r.kick_count == 0]
    elif kicked_norm in KICKED_WITHIN_SECONDS:
        cutoff = now - KICKED_WITHIN_SECONDS[kicked_norm]
        out = [r for r in out if r.last_kick_ts is not None and r.last_kick_ts > cutoff]

    allow_norm = allowlist.strip().lower()
    if allow_norm == "yes":
        out = [r for r in out if r.allowlisted]
    elif allow_norm == "no":
        out = [r for r in out if not r.allowlisted]

    q_norm = q.strip().lower()
    if q_norm:
        out = [r for r in out if q_norm in r.mac.lower() or q_norm in (r.name or "").lower()]

    return out


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


def device_summary(
    conn: sqlite3.Connection,
    *,
    mac: str,
    allowlist: set[str],
    now: float,
) -> DeviceSummary:
    """Stats for the tiles above one device's timeline.

    Same conventions as list_devices: state and kick count come from REAL
    kicks only (dry_run=0), so this header always agrees with the device's
    row on /devices; dry-runs surface separately as a would-kick count.
    MAC matching is case-insensitive to match device_history.
    """
    kick_count, last_kick_ts = conn.execute(
        "SELECT COUNT(*), MAX(ts) FROM kick_events WHERE mac = ? COLLATE NOCASE AND dry_run = 0",
        (mac,),
    ).fetchone()
    (dry_run_count,) = conn.execute(
        "SELECT COUNT(*) FROM kick_events WHERE mac = ? COLLATE NOCASE AND dry_run = 1",
        (mac,),
    ).fetchone()

    latest = conn.execute(
        "SELECT ts, signal, radio FROM client_samples "
        "WHERE mac = ? COLLATE NOCASE ORDER BY ts DESC LIMIT 1",
        (mac,),
    ).fetchone()
    last_seen_ts, signal, radio = latest if latest else (None, None, None)

    # Latest controller-reported name, queried separately so a pre-`name`-column
    # DB (partial deploy) degrades to no name instead of losing the whole header.
    name = None
    try:
        row = conn.execute(
            "SELECT name FROM client_samples WHERE mac = ? COLLATE NOCASE ORDER BY ts DESC LIMIT 1",
            (mac,),
        ).fetchone()
        if row:
            name = row[0]
    except sqlite3.OperationalError:
        name = None

    return DeviceSummary(
        mac=mac,
        name=name,
        state=derive_state(kick_count=kick_count, last_kick_ts=last_kick_ts, now=now),
        allowlisted=mac.lower() in {m.lower() for m in allowlist},
        kick_count=kick_count,
        dry_run_count=dry_run_count,
        last_kick_ts=last_kick_ts,
        last_seen_ts=last_seen_ts,
        signal=signal,
        radio=radio,
    )


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


def _latest_client_names(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Latest controller-reported name per MAC. Degrades to {} if the daemon
    hasn't run the client_samples `name`-column migration yet (partial deploy)."""
    try:
        return {
            mac: name
            for mac, name in conn.execute(
                "SELECT mac, name FROM client_samples "
                "WHERE id IN (SELECT MAX(id) FROM client_samples GROUP BY mac)"
            )
        }
    except sqlite3.OperationalError:
        return {}


def dns_source_health(
    conn: sqlite3.Connection, *, volume_points: int = 24
) -> list[DnsSourceHealth]:
    """Latest heartbeat per Pi-hole instance + a recent query-volume series (ADR-0012).

    Returns [] — rather than raising — when dns_source_samples doesn't exist yet
    (daemon not upgraded / feature off), so the page renders an empty state.
    """
    try:
        latest = conn.execute(
            "SELECT source_name, ts, ok, query_count, error FROM dns_source_samples "
            "WHERE id IN (SELECT MAX(id) FROM dns_source_samples GROUP BY source_name)"
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    out: list[DnsSourceHealth] = []
    for name, ts, ok, query_count, error in sorted(latest, key=lambda r: r[0]):
        volume = [
            int(v or 0)
            for (v,) in conn.execute(
                "SELECT query_count FROM dns_source_samples WHERE source_name = ? "
                "ORDER BY ts DESC LIMIT ?",
                (name, volume_points),
            )
        ]
        out.append(
            DnsSourceHealth(
                name=name,
                ok=bool(ok),
                query_count=query_count or 0,
                last_ts=ts,
                error=error,
                volume=tuple(reversed(volume)),
            )
        )
    return out


def dns_near_threshold(conn: sqlite3.Connection) -> list[DnsObservation]:
    """The latest poll's near-threshold standings, noisiest first (ADR-0012).

    Reads only the newest snapshot (max ts) so the table shows current contenders,
    not historical ones. Empty list when the table is absent.

    Freshness gate: the scanner writes observation rows only when a poll has
    contenders, but writes a source-health heartbeat *every* poll. So if the
    newest heartbeat is more recent than the newest observation, the current poll
    had zero contenders and the latest observations are stale — return [] rather
    than presenting a days-old snapshot as "current."
    """
    try:
        (max_obs_ts,) = conn.execute("SELECT MAX(ts) FROM dns_thrash_observations").fetchone()
    except sqlite3.OperationalError:
        return []
    if max_obs_ts is None:
        return []
    try:
        (max_health_ts,) = conn.execute("SELECT MAX(ts) FROM dns_source_samples").fetchone()
    except sqlite3.OperationalError:
        max_health_ts = None
    if max_health_ts is not None and max_obs_ts < max_health_ts:
        return []

    rows = conn.execute(
        "SELECT mac, domain, query_count, threshold, over_since "
        "FROM dns_thrash_observations WHERE ts = ?",
        (max_obs_ts,),
    ).fetchall()

    names = _latest_client_names(conn)
    obs = [
        DnsObservation(
            mac=mac,
            domain=domain,
            count=count,
            threshold=threshold,
            over_since=over_since,
            name=names.get(mac),
        )
        for mac, domain, count, threshold, over_since in rows
    ]
    obs.sort(key=lambda o: o.count, reverse=True)
    return obs


def dns_thrash_kicks(conn: sqlite3.Connection, *, limit: int = 20) -> list[DnsKick]:
    """Recent kicks that DNS-thrash detection triggered (ADR-0012).

    Tolerates a pre-ADR-0012 kick_events table with no `trigger` column (returns
    [] rather than 500-ing) for the partial-deploy window.
    """
    try:
        rows = conn.execute(
            "SELECT ts, mac, mechanism, dry_run FROM kick_events "
            "WHERE trigger = 'dns_thrash' ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    names = _latest_client_names(conn)
    return [
        DnsKick(ts=ts, mac=mac, mechanism=mechanism, dry_run=bool(dry_run), name=names.get(mac))
        for ts, mac, mechanism, dry_run in rows
    ]
