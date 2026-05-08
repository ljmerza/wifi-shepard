"""AC-6: src/wifi_shepard_ui/ contains zero write-route decorators."""

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


def test_ac_6_no_write_route_decorators_in_ui_source() -> None:
    assert UI_SRC.is_dir(), f"{UI_SRC} must exist"

    write_matches: list[str] = []
    has_any_get_route = False
    for py_file in sorted(UI_SRC.rglob("*.py")):
        text = py_file.read_text()
        if GET_VERB_RE.search(text):
            has_any_get_route = True
        for lineno, line in enumerate(text.splitlines(), start=1):
            if WRITE_VERB_RE.search(line):
                write_matches.append(
                    f"{py_file.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
                )

    # Sanity: AC-6 is a guardrail — it's only meaningful once the UI defines
    # routes. Without GET routes, the negative assertion is vacuous.
    assert has_any_get_route, (
        "AC-6 sanity check: src/wifi_shepard_ui/ must define at least one GET route "
        "for the no-write-routes guarantee to be meaningful"
    )

    assert not write_matches, (
        "v1 sidecar must be read-only — found write-route decorators:\n  "
        + "\n  ".join(write_matches)
    )
