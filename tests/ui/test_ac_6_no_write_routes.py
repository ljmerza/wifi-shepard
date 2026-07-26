"""ADR-0002 AC-6, as amended by ADR-0013 and ADR-0014: src/wifi_shepard_ui/ contains no
write-route decorators EXCEPT the settings save (`@app.post("/settings")`) and the
per-device save (`@app.post("/devices/{mac}/settings")`). Every other route stays
GET-only — the read-only guarantee is narrowed to a two-path allowlist, not lifted.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_SRC = REPO_ROOT / "src" / "wifi_shepard_ui"

# Match @app.post(...), @router.put(...), etc. The leading @ is the load-bearing
# part; we tolerate any object name (`app`, `router`, ...) before the verb so
# that future refactors using APIRouter still get caught.
WRITE_VERB_RE = re.compile(
    r"@[A-Za-z_][A-Za-z0-9_]*\.(post|put|delete|patch)\s*\(",
    re.IGNORECASE,
)

GET_VERB_RE = re.compile(r"@[A-Za-z_]\w*\.get\s*\(")

# The write routes the ADRs permit: the ADR-0013 settings save, and the ADR-0014
# per-device save. Anything else is still a violation.
ALLOWED_WRITE_RE = re.compile(
    r"""@[A-Za-z_]\w*\.post\s*\(\s*["'](/settings|/devices/\{mac\}/settings)["']\s*\)"""
)


def test_ac_6_only_settings_write_route_in_ui_source() -> None:
    assert UI_SRC.is_dir(), f"{UI_SRC} must exist"

    write_matches: list[str] = []
    has_any_get_route = False
    saw_allowed_settings_post = False
    for py_file in sorted(UI_SRC.rglob("*.py")):
        text = py_file.read_text()
        if GET_VERB_RE.search(text):
            has_any_get_route = True
        for lineno, line in enumerate(text.splitlines(), start=1):
            if WRITE_VERB_RE.search(line):
                if ALLOWED_WRITE_RE.search(line):
                    saw_allowed_settings_post = True
                    continue
                write_matches.append(f"{py_file.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert has_any_get_route, (
        "sanity check: src/wifi_shepard_ui/ must define at least one GET route "
        "for the no-write-routes guarantee to be meaningful"
    )

    assert not write_matches, (
        "sidecar must be read-only outside the settings and per-device save routes — "
        "found other write-route decorators:\n  " + "\n  ".join(write_matches)
    )

    # Guards against this test silently going stale: the amended allowlist route
    # must actually be present.
    assert saw_allowed_settings_post, (
        'expected the ADR-0013 settings save route @app.post("/settings") in the UI source'
    )
