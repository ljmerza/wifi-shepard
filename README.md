# wifi-shepard

A long-running Docker daemon that watches a wireless network and gently
re-roams misbehaving clients so cheap IoT (Espressif WLEDs, smart plugs,
off-brand cameras) stop monopolizing 2.4 GHz airtime by clinging to the wrong AP.

Built around a brand-agnostic `Controller` interface (UniFi first; Omada /
OpenWRT / Ruckus / Aruba slot in as new backends). The full v1 spec — detection
rules, backoff schedule, roadmap — lives in [`PLAN.md`](./PLAN.md).

An HTTP UI sidecar (`wifi-shepard-ui`) renders device history and a WiFi status
overview from the daemon's SQLite state, and — via a **Settings** page
([ADR-0013](./docs/adr/0013-settings-ui-write-paths.md)) — lets you edit the
daemon's whole configuration in the browser, with a plain-English explanation on
every threshold. Secrets are never typed in the UI: you name the environment
variable that holds each one (e.g. `UNIFI_PASSWORD`) and the daemon resolves it.
Saved changes are validated with the daemon's own parser, written back to
`config.yaml`, and hot-reloaded (no restart) for anything but connection/
notification wiring.

## Get started

### 1. Configure

```bash
# from the wifi-shepard repo root
cp config.example.yaml config.yaml
cp env.example wifi-shepard.env
cp wifi-shepard-ui.env.example wifi-shepard-ui.env
```

Edit:

- `config.yaml` — controller connection (non-secret fields are plain literals now), allowlist, detection thresholds. Keep `scanner.dry_run: true` until you have watched the logs for a poll cycle or two. Once running, most of this is editable from the UI's **Settings** page instead of by hand.
- `wifi-shepard.env` — **secrets only**: `UNIFI_PASSWORD` (required), `HA_TOKEN` / `PIHOLE_PASSWORD` (optional). Non-secret UniFi fields (host/username/site/port) live in `config.yaml`.
- `wifi-shepard-ui.env` — `WIFI_SHEPARD_UI_TOKEN` (bearer token; unset to disable auth — **set it before exposing the UI, since Settings can now write config**), `WIFI_SHEPARD_UI_REFRESH_SECONDS` (overview auto-refresh interval, default 60s; 0 disables).

### 2. Pull or build the images

CI publishes both images to GHCR on every push to `main` and on tagged
releases. Pull them directly:

```bash
docker pull ghcr.io/ljmerza/wifi-shepard:main
docker pull ghcr.io/ljmerza/wifi-shepard-ui:main
# or a pinned release once one exists:
# docker pull ghcr.io/ljmerza/wifi-shepard:v0.1.0
```

Or build locally from the repo:

```bash
docker build -t wifi-shepard:dev .
docker build -t wifi-shepard-ui:dev -f Dockerfile.ui .
```

### 3. Run

This repo lives inside a docker-compose monorepo at
`/media/cubxi/docker`. The compose fragment in
[`docker-compose.yml`](./docker-compose.yml) is wired into the monorepo's
graph via the `dca` wrapper:

```bash
# from the monorepo root
dca config                              # validate the merged compose graph
dca up -d wifi-shepard wifi-shepard-ui
dca logs -f wifi-shepard
dca restart wifi-shepard                # after editing config.yaml
```

Standalone (outside the monorepo) — copy the service blocks out of
`docker-compose.yml` into your own compose file, replace the
`*default-logging` anchor with whatever logging driver you want, and run
`docker compose up -d`.

The daemon writes its SQLite state to `/data/state.db` (mount as a volume) and
reads `/config/config.yaml`. The UI sidecar mounts the state volume read-only and
the config directory read-write (so the Settings page and the per-device cards can
save), and serves on port 8080. Two routes write `config.yaml`: `POST /settings`
(the whole config) and `POST /devices/{mac}/settings` (just that device's
allowlist / override / inactivity / reboot settings). Every other route is GET-only,
enforced at startup. The config is mounted as a **directory** (not a single file) so the
daemon's file-watch reload sees edits — a single-file bind mount pins the inode and
misses atomic rewrites.

## Local development

Requires [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (the Python package manager).

```bash
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

CI runs the same lint + test commands via `ljmerza/misc-actions`. Frontend / UI
tests use FastAPI's `TestClient`; no browser or network is required.

## Architecture decisions

ADRs live in [`docs/adr/`](./docs/adr/). The index is
[`0000-adr-index.md`](./docs/adr/0000-adr-index.md).

## CI / release

- **PRs** → ruff + pytest, plus PR-tagged image builds to GHCR
  (`ghcr.io/ljmerza/wifi-shepard{,-ui}:pr-<N>`). PR images are deleted when
  the PR closes.
- **Push to `main`** → tests + push `main`-tagged image with SLSA provenance.
- **GitHub Release** → pushes a versioned image (`v1.2.3`) plus `latest`.

Cut a release: `git tag vX.Y.Z && git push --tags`, then create a GitHub
Release pointing at the tag — both images publish under `vX.Y.Z` and
`latest`.
