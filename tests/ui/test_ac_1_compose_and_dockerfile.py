"""AC-1: compose fragment defines wifi-shepard-ui sidecar; Dockerfile.ui exists."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# The fragment uses *default-logging, which is anchored in the monorepo's
# docker-compose.base.yml. The fragment is NOT a standalone valid YAML
# document. Prefix a dummy anchor so safe_load resolves it during the test.
ANCHOR_SHIM = "x-test-anchor: &default-logging {}\n"


def _load_fragment() -> dict:
    fragment_path = REPO_ROOT / "docker-compose.fragment.yml"
    return yaml.safe_load(ANCHOR_SHIM + fragment_path.read_text())


def test_ac_1_compose_fragment_defines_ui_sidecar() -> None:
    fragment = _load_fragment()
    assert isinstance(fragment, dict), "fragment must be a YAML mapping"
    assert "wifi-shepard-ui" in fragment, (
        "compose fragment must define a wifi-shepard-ui service alongside wifi-shepard"
    )
    ui = fragment["wifi-shepard-ui"]
    volumes = ui.get("volumes", []) or []
    assert any(":/data:ro" in v for v in volumes), (
        f"wifi-shepard-ui must mount /data read-only; got volumes={volumes!r}"
    )
    assert "healthcheck" in ui, "wifi-shepard-ui must declare a healthcheck"
    build = ui.get("build")
    assert isinstance(build, dict), (
        "wifi-shepard-ui must use dict-form build: to specify Dockerfile.ui"
    )
    assert build.get("dockerfile") == "Dockerfile.ui", (
        f"wifi-shepard-ui build.dockerfile must be 'Dockerfile.ui'; got {build!r}"
    )


def test_ac_1_dockerfile_ui_exists() -> None:
    assert (REPO_ROOT / "Dockerfile.ui").is_file(), (
        "Dockerfile.ui must exist at repo root for the UI sidecar build"
    )
