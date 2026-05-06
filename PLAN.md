# wifi-shepard

A Docker container that watches a wireless network and gently re-roams misbehaving clients so cheap IoT (Espressif WLEDs, smart plugs, off-brand cameras) stop monopolizing 2.4 GHz airtime by clinging to the wrong AP.

**Shape**: long-running daemon, no CLI, no UI in v1. Configuration via YAML file mounted into the container. All output to stdout (collected by Docker's log driver). Notifications via Home Assistant REST.

**Brand-agnostic**: built around a `Controller` interface. UniFi is the first implementation; TP-Link Omada / OpenWRT / Ruckus / Aruba can be added later as new classes without changing the rest of the daemon.

## 1. Why this exists

This session ran a full manual pass on a 5-AP UniFi network. Findings that justify automating:

- A single misassociated WLED (1 Mbps PHY, 43% retry) was eating ~12% of one AP's airtime.
- A WiFi camera glued to Front Porch AP at 6 Mbps moved to Back Porch 5 GHz on a single force-reconnect → **96× faster TX rate, ~4000× lower airtime cost**.
- A whole-house AP reboot only partially fixed things; ~30% of clients drifted back to the wrong AP within minutes.
- Manual diagnosis (this session: 2 hours, 13 kicks, channel re-plan, tx_power changes) is tedious. The post-fix steady state can be maintained automatically.

The goal: a small always-on container that recognizes `low PHY × high retry × better AP exists nearby` and force-reconnects the offender — with safety rails so it doesn't kick-loop on devices that just suck.

## 2. What it does (v1 scope)

```
loop every 60s:
  for each wireless client on the controller:
    pull wifi_details (signal, tx_rate, retries, attempts, bssid)
    push into a sliding window (last N samples)
    if window meets "bad-state" criteria (per-MAC override > global default):
      check backoff state for this MAC
      if eligible:
        force-reconnect via Controller backend
        record kick event
        bump backoff
        send HA notification
```

That's the whole loop. Everything else is rules around it.

## 3. Detection criteria

Composite trigger — a client must hit **all** of these for a sustained window (≥ 5 minutes / 5 consecutive samples):

| Signal | Default threshold | Why |
|---|---|---|
| `tx_rate` | < 12 Mbps | Below this on 2.4 GHz means very poor PHY, lots of airtime per byte |
| `tx_retries / wifi_tx_attempts` | > 30% | Half the frames are retransmits — link is unstable |
| `signal` | < -70 dBm | Could likely find a stronger AP |
| `radio` | == "ng" | Most of the airtime damage happens on 2.4 GHz |

Plus an **AP-side gate**: only act if the AP this client is on is currently `cu_total > 60%`. If the AP has plenty of headroom, the slow client isn't hurting anyone.

**Threshold resolution**: every threshold has a global default (in `detection:`) plus optional per-MAC override (in `overrides:`). Resolution is per-MAC override > global default. So a phone you don't want kicked aggressively can have its own `tx_rate_kbps_max: 6000`, while every other client uses the 12 Mbps default. All thresholds support this.

## 4. Action policy and backoff

Per-MAC state machine:

```
NORMAL ──(bad window detected)──▶ KICK_PENDING
KICK_PENDING ──(within budget)──▶ KICKED ──(wait cooldown)──▶ EVALUATING
EVALUATING ──(bad again)──▶ KICK_PENDING (longer cooldown)
EVALUATING ──(better)──▶ NORMAL
KICK_PENDING ──(over budget)──▶ QUARANTINE (notify, don't kick)
```

Backoff schedule (configurable in YAML):

| Kick # | Cooldown before next kick allowed |
|---|---|
| 1 | 5 min |
| 2 | 30 min |
| 3 | 2 h |
| 4 | 12 h |
| 5+ | 24 h, also send "this device may be defective" notification |

Hard caps (also overridable per MAC):
- Max 3 kicks per device per hour
- Max 10 kicks per device per day
- Quiet hours (default 23:00–07:00 local): no kicks unless device is *currently* destroying airtime (configurable `override_threshold` in YAML)

Allowlist: MACs from YAML that are never kicked.

## 5. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Async | asyncio (single event loop) |
| Controller backends | Pluggable `Controller` Protocol; `UniFiController` first; `aiounifi` library under the hood |
| Local state | SQLite via `aiosqlite` (WAL mode), used for kick history and backoff timers |
| Config | YAML file mounted at `/config/config.yaml`, parsed via `pydantic-settings` with env var interpolation |
| Logging | `structlog` (JSON or human-readable, configurable) → stdout → Docker log driver |
| Notifications | HA REST `/api/services/notify/<service>` |
| Container base | `python:3.12-slim` |
| Process | Single python process, no supervisord, no celery, no redis |

No web framework, no UI, no CLI in v1. The container starts, runs the loop, logs, exits cleanly on SIGTERM. Health is observable via Docker container status + log lines.

## 6. Brand-agnostic Controller interface

Every backend implements the same Protocol so the scanner/scorer/actor never know which vendor they're talking to.

```python
# src/wifi_shepard/controllers/base.py
from typing import Protocol

class Controller(Protocol):
    async def list_wireless_clients(self) -> list[ClientSnapshot]: ...
    async def list_aps(self) -> list[APSnapshot]: ...
    async def get_ap_radio_stats(self, ap_id: str) -> list[RadioStats]: ...
    async def force_reconnect_client(self, mac: str) -> None: ...
    # optional, when supported by the vendor:
    async def send_btm_request(self, mac: str, target_bssid: str | None = None) -> None: ...
```

```python
# src/wifi_shepard/controllers/unifi.py
class UniFiController:
    def __init__(self, host, username, password, site, verify_ssl): ...
    async def list_wireless_clients(self) -> list[ClientSnapshot]: ...
    # ...

# Future:
# class OmadaController: ...
# class OpenWRTController: ...
# class RuckusController: ...
```

Backend selection in YAML:

```yaml
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    ...
  # - type: omada
  #   name: shop
  #   host: 192.168.5.1
  #   ...
```

Multiple controllers per daemon is supported by design (run scanner concurrently per controller). v1 just ships UniFi.

## 7. Configuration (`config.yaml`)

Single source of truth. Mounted into the container; the daemon reads it at startup and on SIGHUP.

```yaml
controllers:
  - type: unifi
    name: home
    host: 192.168.1.1
    username: shepard
    password: ${UNIFI_PASSWORD}     # env var interpolation
    site: default
    verify_ssl: false

home_assistant:
  url: http://homeassistant:8123
  token: ${HA_TOKEN}
  notify_service: mobile_app_pixel

scanner:
  poll_interval_seconds: 60
  window_samples: 5               # 5 samples = 5 minutes at 60s polling
  log_level: info
  log_format: human               # or "json"
  dry_run: true                   # start safe; flip to false when you trust it

detection:                        # global defaults
  tx_rate_kbps_max: 12000
  retry_pct_max: 30
  signal_dbm_max: -70
  radios: [ng]                    # only 2.4 GHz by default
  ap_cu_total_min: 60             # only act on saturated APs

backoff:
  cooldowns_seconds: [300, 1800, 7200, 43200, 86400]
  max_kicks_per_hour: 3
  max_kicks_per_day: 10
  quarantine_after_kicks: 5

quiet_hours:
  start: "23:00"
  end: "07:00"
  timezone: America/Chicago
  override_threshold:             # only kick if these *stricter* thresholds hit during quiet hours
    tx_rate_kbps_max: 2000
    retry_pct_max: 50
    ap_cu_total_min: 80

allowlist:                        # MACs never kicked
  - aa:bb:cc:dd:ee:ff             # work laptop
  - 11:22:33:44:55:66             # baby monitor

overrides:                        # per-MAC tweaks; resolution: override > global
  - mac: dc:cc:e6:66:86:2b
    name: "leonardo s22"
    tx_rate_kbps_max: 6000        # phone, more lenient PHY threshold
    radios: [ng, na, 6e]          # also act on 5/6 GHz for this device
  - mac: 64:57:25:81:89:ab
    name: "back bedroom camera"
    max_kicks_per_day: 3          # tighter budget for this one
    signal_dbm_max: -65           # stricter — kick sooner
```

Secrets (`UNIFI_PASSWORD`, `HA_TOKEN`) live in env vars (loaded from `./env/wifi-shepard.env`) and are interpolated at parse time.

## 8. Architecture

```
┌──────────────────┐
│  UniFi UDM Pro   │ 192.168.1.1
└────────┬─────────┘
         │ aiounifi (HTTPS)        (future: aioomada, opwrt-rpc, ...)
         ▼
┌──────────────────────────────┐    ┌────────────────────┐
│  wifi-shepard container      │───▶│  Home Assistant    │
│  ┌────────────────────────┐  │    │  /api/services/... │
│  │ Controller backends    │  │    └────────────────────┘
│  │   - UniFiController    │  │
│  │ scanner   (asyncio)    │  │
│  │ scorer    (per-MAC win)│  │
│  │ actor     (kick/notify)│  │
│  │ backoff   (state mgr)  │  │
│  │ config    (YAML loader)│  │
│  └───────────┬────────────┘  │
└──────────────┼───────────────┘
               ▼
       ┌──────────────┐
       │  SQLite      │  /data/state.db
       │  (WAL mode)  │
       └──────────────┘
```

Single container, one event loop, no IPC. Configuration in, decisions out via logs and HA notifications.

## 9. Repo layout

```
projects/wifi-shepard/
├── PLAN.md
├── README.md                    # operating runbook (after v1)
├── pyproject.toml               # ruff + uv-managed deps
├── Dockerfile                   # python:3.12-slim base
├── docker-compose.fragment.yml  # to merge into docker-compose.local.yml
├── env.example
├── config.example.yaml
├── src/wifi_shepard/
│   ├── __init__.py
│   ├── main.py                  # entry point, signal handling, top-level loop
│   ├── config.py                # pydantic-settings, YAML loader, env interp
│   ├── controllers/
│   │   ├── base.py              # Controller Protocol, ClientSnapshot model
│   │   ├── unifi.py             # UniFiController (aiounifi)
│   │   └── __init__.py          # backend factory keyed on YAML "type"
│   ├── scanner.py               # poll loop
│   ├── scorer.py                # bad-state detection, threshold resolution
│   ├── backoff.py               # per-MAC state machine
│   ├── actor.py                 # force_reconnect call
│   ├── notify/
│   │   └── ha.py
│   ├── db.py                    # aiosqlite session, migrations
│   └── models.py                # pydantic models
└── tests/
    ├── test_scorer.py
    ├── test_backoff.py
    └── test_threshold_resolution.py
```

## 10. Deployment in the docker monorepo

Per `/media/cubxi/docker/CLAUDE.md`, new in-progress services live in `docker-compose.local.yml`. Move to `docker-compose.home.yml` once stable.

```yaml
wifi-shepard:
  build: ./projects/wifi-shepard
  container_name: wifi-shepard
  restart: unless-stopped
  env_file:
    - ./env/global.env
    - ./env/wifi-shepard.env
  volumes:
    - ./volumes/wifi-shepard:/data
    - ./projects/wifi-shepard/config.yaml:/config/config.yaml:ro
  logging: *default-logging
```

Env file (`./env/wifi-shepard.env`):

```
UNIFI_PASSWORD=...
HA_TOKEN=...
```

No exposed ports. Logs via `dca logs -f wifi-shepard`.

## 11. Roadmap

### v0 — observe-only (1–2 evenings)

- Poll every 60s, write per-client samples to SQLite.
- Implement scorer; log "I would have kicked X" decisions.
- `dry_run: true` enforced regardless of config.
- Goal: validate thresholds against real data for ~1 week before enabling actions.

### v1 — actions on (a weekend)

- Honor `dry_run` flag.
- Enable `force_reconnect` with backoff.
- HA REST notification per kick + on quarantine.
- SIGHUP reloads YAML.
- UniFi backend only.

### v2+ — see brainstorm below

## 12. Future features (brainstorm — not committing, just capturing)

### Smarter detection
- Roam-recommendation engine: cross-reference current BSS with neighbor scan; only kick if a better AP demonstrably exists.
- Per-device profile / fingerprinting: learn baselines per MAC, alert on drift.
- BTM-aware roaming: send 802.11v BSS Transition Management request before kicking — much gentler than a deauth on capable clients.
- 802.11k/v/r capability detection per client; use the gentlest mechanism the client supports.
- Boot-time detection: notice just-associated clients; if they pick a clearly-wrong AP within 60s, kick immediately (no backoff).
- Sticky-client detection: hasn't roamed in N hours despite RSSI changes.
- Bandwidth quota anomaly: flag IoT that suddenly uploads 10× their baseline (possible compromise).
- TX power oscillation detection (UniFi auto-power flapping).
- Rogue AP / evil twin SSID detection.
- DHCP lease anomaly (renew loop).
- Device fingerprint tracking (model/firmware change without MAC change → flag).
- Mass-disassoc detector (firmware push) — pause kicks for 5 min after.
- Per-VLAN behavioral norms (cameras vs phones vs printers).

### Brand-agnostic backends
- Omada (TP-Link) controller backend.
- OpenWRT (uci/ubus over SSH or rpcd) backend.
- Ruckus SmartZone API backend.
- Aruba Instant On / ArubaOS-CX backend.
- Mist API backend.
- pfSense / OPNsense + hostapd backend.
- Generic SNMP fallback for read-only metrics.
- Multi-controller mode: aggregate samples across two or more controllers in one daemon.

### Network-wide insights
- Channel utilization graphs per radio over time.
- AP-aware tx_power suggestions.
- Auto channel re-plan suggestions when neighboring APs share a channel.
- AP firmware mismatch detection.
- AP silent degradation detection (CRC/retry creep).
- Wired switch port saturation correlation.
- DNS health check (poll Pi-holes from each VLAN, alert on timeout).
- Detect "newly joined AP" / unexpected BSSIDs.

### Operator UX
- HA entities via MQTT discovery: `sensor.wifi_shepard_kicks_today`, `binary_sensor.wifi_shepard_quiet_hours`, `switch.wifi_shepard_enabled`, `button.wifi_shepard_kick_<mac>`.
- Schedule-aware: skip kicks during HA "armed away", Frigate detection events, etc.
- Holiday mode: disable kicks while away (reads HA `device_tracker`).
- Per-network (VLAN) thresholds.
- Per-MAC notes/labels in YAML (free text reminder of physical location).
- Notification rate limiting: digest mode ("3 devices kicked in last hour") instead of per-event.
- Kick acknowledgement loop: HA actionable notification with approve/deny before kicking critical devices.
- Manual kick trigger via HA button → MQTT command → daemon executes.
- Per-device weekday/weekend rules.
- Network-wide pause switch (HA `switch.wifi_shepard_paused`).

### Notifications
- Channels: HA (primary), ntfy.sh, Slack, Discord, Telegram, email digest.
- Per-event vs digest mode.
- Severity levels (info / warn / critical) with separate destinations.
- Pre-kick warning (if approval mode on).

### Observability
- Prometheus `/metrics` endpoint (`kick_total`, `quarantine_total`, `airtime_score_per_mac`, `cu_total_per_radio`).
- OpenTelemetry traces for each scan cycle.
- Kick-result classifier: did the kick *help*? Compare pre/post airtime score.
- Anomaly correlation view: when DNS_TIMEOUT spikes for client X, what else degraded?
- Weekly summary report posted to HA / Slack.
- "Why-not-kicked" log line for every candidate that scored bad but was skipped (and why).
- Heartbeat ping to HA so an "alive" sensor can alarm if container dies.

### UI (way down the road)
- FastAPI + HTMX read-only dashboard: current bad-state list, kick history, allowlist, quarantine.
- Floor-plan view: drag AP positions onto SVG, dot clients with current AP.
- Roam map: visualize which AP each device used over the week.
- Historical kick CSV export for the `network/` analysis docs.

### Integration
- Multi-site / multi-vendor controllers in one daemon.
- Read existing `network/wifi-analysis-*.md` to seed allowlist / overrides.
- Push events to Loki for log aggregation.
- Backup/restore of state.db (and which devices are quarantined).
- Webhook test endpoint at startup ("did HA notify actually work?").

### Safety rails
- Single-flight kicks (max 1 action per N seconds globally).
- Per-AP kick cap (don't drain an AP all at once).
- Cooldown after detected mass-disassoc (firmware push).
- Network-wide pause switch.
- Heartbeat / dead-man's-switch.
- Fail-closed YAML validation: invalid config → log + exit, no half-running.

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Kick-loop on a defective device | Exponential backoff + per-day cap + auto-quarantine |
| Kick during a critical moment (alarm, baby monitor) | Allowlist + quiet hours + dry-run start |
| Controller rate-limits / locks us out | Single-flight kicks, max 1 action per N seconds globally |
| AP firmware push or channel re-plan triggers mass re-flag | Mass-disassoc detector pauses kicks for 5 min after big drop event |
| Service down silently | HA `binary_sensor.wifi_shepard_alive` ping; notify if stale |
| Credentials leak | Env-only, no secrets in repo; UniFi creds already in `.claude/settings.local.json` per `network/CLAUDE.md` convention |
| YAML config typo causes the daemon to crash-loop | Fail closed: validate config on startup, log clear error, exit; don't half-run |
| Vendor backend API drift (e.g. UniFi 9.x → 10.x) | Pin `aiounifi` version; integration test against a recorded API fixture; fail closed if schema validation breaks |
