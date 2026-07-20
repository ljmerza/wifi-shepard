"""ADR-0014: per-device settings editing — a device-centric write surface over the
same config.yaml.

One test per acceptance criterion. The route under test is
``POST /devices/{mac}/settings`` with partial-payload semantics (an absent key leaves
that setting unchanged).
"""

from __future__ import annotations

import difflib
import html as htmllib
from pathlib import Path

import pytest

from tests.ui._device_data import (
    ABSENT_SECTIONS,
    ALLOWLISTED_MAC,
    NEW_MAC,
    OVERRIDE_MAC,
    SAMPLE,
    device_client,
    write_device_sample,
)
from tests.ui._settings_data import payload_from
from wifi_shepard.config import load_config_from_path
from wifi_shepard_ui import settings_schema as ss


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIFI_SHEPARD_UI_TOKEN", raising=False)
    # The daemon's re-parse of the written file interpolates ${UNIFI_PASSWORD}.
    monkeypatch.setenv("UNIFI_PASSWORD", "x")


def test_ac_1_allowlist_toggle_adds_removes_and_is_idempotent(tmp_path: Path) -> None:
    cfg = write_device_sample(tmp_path)
    client = device_client(tmp_path, cfg)

    # Add — the MAC lands in allowlist:, normalized to lowercase.
    r = client.post(f"/devices/{NEW_MAC.upper()}/settings", json={"allowlisted": True})
    assert r.status_code == 200, r.text
    assert NEW_MAC in load_config_from_path(cfg).allowlist

    # Idempotent — a repeat add leaves the file byte-for-byte unchanged, no duplicate.
    after_add = cfg.read_bytes()
    r = client.post(f"/devices/{NEW_MAC}/settings", json={"allowlisted": True})
    assert r.status_code == 200, r.text
    assert cfg.read_bytes() == after_add
    assert list(load_config_from_path(cfg).allowlist).count(NEW_MAC) == 1

    # Remove — only the targeted MAC goes; the pre-existing entry survives.
    r = client.post(f"/devices/{NEW_MAC}/settings", json={"allowlisted": False})
    assert r.status_code == 200, r.text
    allowlist = load_config_from_path(cfg).allowlist
    assert NEW_MAC not in allowlist
    assert ALLOWLISTED_MAC in allowlist


def _line_diff(before: str, after: str) -> tuple[list[str], list[str]]:
    """(removed, added) lines between two revisions of the file."""
    before_lines, after_lines = before.splitlines(), after.splitlines()
    removed: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, before_lines, after_lines
    ).get_opcodes():
        if tag == "equal":
            continue
        removed += before_lines[i1:i2]
        added += after_lines[j1:j2]
    return removed, added


# (payload, sections this payload legitimately creates). Everything in ABSENT_SECTIONS
# that isn't listed here must still be absent afterwards.
_SURGICAL_CASES = [
    ({"allowlisted": True}, ()),
    ({"inactivity_watched": True}, ("inactivity:",)),
    ({"reboot_eligible": True}, ("reboot:",)),
    ({"reboot_override": {"ha_entity": "switch.kitchen_plug"}}, ("reboot:",)),
    ({"overrides": {"signal_dbm_max": -80}}, ()),
]


@pytest.mark.parametrize("payload,creates", _SURGICAL_CASES)
def test_ac_2_save_touches_only_the_edited_key(
    tmp_path: Path, payload: dict, creates: tuple[str, ...]
) -> None:
    cfg = write_device_sample(tmp_path)
    before = cfg.read_text()

    r = device_client(tmp_path, cfg).post(f"/devices/{NEW_MAC}/settings", json=payload)
    assert r.status_code == 200, r.text
    after = cfg.read_text()

    # A surgical write never conjures a section this payload has no business creating.
    for section in ABSENT_SECTIONS:
        if section in creates:
            continue
        assert section not in after, f"{section} was materialized by {payload}"

    assert "# operator hand comment" in after  # comments preserved
    assert "${UNIFI_PASSWORD}" in after  # secret placeholder never resolved

    # Nothing is ever *removed*, and every added line belongs to the edit.
    removed, added = _line_diff(before, after)
    assert removed == [], f"a surgical save removed lines: {removed}"
    assert added, "the save should have added something"
    assert len(added) <= 4, f"expected a small addition, got: {added}"

    # Unrelated settings still parse to their original values.
    cfg_obj = load_config_from_path(cfg)
    assert cfg_obj.detection.signal_dbm_max == -70
    assert cfg_obj.scanner.poll_interval_seconds == 60


def test_ac_2_no_op_save_never_rewrites_the_file(tmp_path: Path) -> None:
    """A payload that changes nothing must not touch the file at all — re-emitting an
    untouched document is how an unrelated reflow would sneak in."""
    cfg = write_device_sample(tmp_path)
    before = cfg.read_bytes()
    client = device_client(tmp_path, cfg)

    for payload in ({}, {"allowlisted": False}, {"overrides": {}}):
        r = client.post(f"/devices/{NEW_MAC}/settings", json=payload)
        assert r.status_code == 200, r.text
        assert cfg.read_bytes() == before, f"{payload} rewrote the file"


def test_ac_2_flat_sequence_style_is_preserved(tmp_path: Path) -> None:
    """ruamel has no per-file style memory, so the writer must take the file's own list
    indentation — otherwise an edit reindents every sequence it never touched."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(SAMPLE.replace("\n  - ", "\n- ").replace("\n    ", "\n  "))
    before = cfg.read_text()

    r = device_client(tmp_path, cfg).post(
        f"/devices/{NEW_MAC}/settings", json={"allowlisted": True}
    )
    assert r.status_code == 200, r.text

    removed, added = _line_diff(before, cfg.read_text())
    assert removed == [], f"a flat-style config was reindented: {removed}"
    assert len(added) == 1 and NEW_MAC in added[0]


def _override_for(cfg: Path, mac: str):
    return next((o for o in load_config_from_path(cfg).overrides if o.mac == mac), None)


def test_ac_3_absent_key_unchanged_null_clears_empty_row_removed(tmp_path: Path) -> None:
    cfg = write_device_sample(tmp_path)
    client = device_client(tmp_path, cfg)

    # An explicit null clears one knob; the knobs NOT in the payload are untouched,
    # and so is every setting outside `overrides` (partial-payload semantics).
    r = client.post(
        f"/devices/{OVERRIDE_MAC}/settings", json={"overrides": {"tx_rate_kbps_max": None}}
    )
    assert r.status_code == 200, r.text
    row = _override_for(cfg, OVERRIDE_MAC)
    assert row is not None
    assert row.tx_rate_kbps_max is None  # cleared -> inherits the global
    assert row.signal_dbm_max == -65  # absent from the payload -> unchanged
    assert "leonardo s22" in cfg.read_text()  # cosmetic label untouched
    assert ALLOWLISTED_MAC in load_config_from_path(cfg).allowlist

    # Clearing the last of a row's fields removes the row rather than leaving a
    # `- mac: ...` stub behind.
    r = client.post(
        f"/devices/{OVERRIDE_MAC}/settings",
        json={"overrides": {"signal_dbm_max": None, "name": None}},
    )
    assert r.status_code == 200, r.text
    assert _override_for(cfg, OVERRIDE_MAC) is None
    assert OVERRIDE_MAC not in cfg.read_text()


def test_ac_4_device_card_renders_every_per_mac_field_from_the_schema(tmp_path: Path) -> None:
    cfg = write_device_sample(tmp_path)
    # No DB: a device with zero recorded history must still be configurable.
    r = device_client(tmp_path, cfg).get(f"/devices/{OVERRIDE_MAC}")
    assert r.status_code == 200
    page = htmllib.unescape(r.text)

    # The three MAC-list memberships render as per-device toggles, with the label and
    # description taken from the schema (no metadata duplicated in the template).
    assert ss.PER_DEVICE_MEMBERSHIPS, "schema must declare the per-device memberships"
    for membership in ss.PER_DEVICE_MEMBERSHIPS:
        field = ss.field_by_path(membership.path)
        assert field is not None, f"{membership.path} must be a FieldSpec"
        assert membership.label in page
        assert field.description in page
        assert f'data-device-key="{membership.key}"' in r.text

    # Every per-MAC object-list knob renders too — minus `mac`, which is the URL.
    assert ss.PER_DEVICE_OBJECT_LISTS, "schema must declare the per-device object lists"
    for key, prefix in ss.PER_DEVICE_OBJECT_LISTS:
        leaves = [f for f in ss.item_fields(prefix) if f.path != f"{prefix}mac"]
        assert leaves, f"{prefix} must contribute editable leaves"
        for field in leaves:
            leaf = field.path[len(prefix) :]
            assert f'data-device-leaf="{key}:{leaf}"' in r.text, f"{key}:{leaf} not rendered"
            assert field.label in page

    # Identity comes from the URL, never an editable input — and the server enforces
    # it, so a request for one device can't re-point another device's row.
    assert 'data-device-leaf="overrides:mac"' not in r.text
    for group in ("overrides", "reboot_override"):
        rejected = device_client(tmp_path, cfg).post(
            f"/devices/{OVERRIDE_MAC}/settings", json={group: {"mac": "99:99:99:99:99:99"}}
        )
        assert rejected.status_code == 400, f"{group}.mac must not be editable"
        assert "99:99:99:99:99:99" not in cfg.read_text()

    # Pre-filled from the live config.yaml.
    assert 'value="6000"' in r.text  # overrides[].tx_rate_kbps_max
    assert 'value="-65"' in r.text  # overrides[].signal_dbm_max


def test_ac_5_invalid_save_rejected_with_daemon_message_file_unchanged(tmp_path: Path) -> None:
    cfg = write_device_sample(tmp_path)
    before = cfg.read_bytes()

    r = device_client(tmp_path, cfg).post(
        f"/devices/{OVERRIDE_MAC}/settings", json={"overrides": {"kick_mechanism": "bogus"}}
    )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    # The daemon's own message, produced by validating the WHOLE mutated config.
    assert "kick_mechanism" in body["error"]
    assert "bogus" in body["error"]
    assert cfg.read_bytes() == before


DEVICE_SETTINGS_ROUTE = "/devices/{mac}/settings"


def test_ac_6_write_fence_allows_exactly_two_paths_and_gates_on_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from wifi_shepard_ui.app import (
        _ALLOWED_WRITE_PATHS,
        _assert_no_write_routes,
        create_app,
    )

    assert set(_ALLOWED_WRITE_PATHS) == {"/settings", DEVICE_SETTINGS_ROUTE}

    cfg = write_device_sample(tmp_path)
    app = create_app(db_path=tmp_path / "absent.db", config_path=cfg)
    write_methods = {"POST", "PUT", "DELETE", "PATCH"}
    actual_write_paths = {
        route.path
        for route in app.routes
        if write_methods & {m.upper() for m in (getattr(route, "methods", None) or set())}
    }
    assert actual_write_paths == {"/settings", DEVICE_SETTINGS_ROUTE}

    # The fence is still a fence: an unlisted write route raises at startup.
    @app.post("/rogue")
    def _rogue() -> dict[str, bool]:  # pragma: no cover - never called
        return {"ok": True}

    with pytest.raises(RuntimeError, match="rogue"):
        _assert_no_write_routes(app)

    # With a token configured, an unauthenticated per-device save is refused and the
    # file is untouched (same posture as the settings route).
    monkeypatch.setenv("WIFI_SHEPARD_UI_TOKEN", "s3cret")
    before = cfg.read_bytes()
    client = TestClient(create_app(db_path=tmp_path / "absent.db", config_path=cfg))
    r = client.post(f"/devices/{NEW_MAC}/settings", json={"allowlisted": True})
    assert r.status_code == 401
    assert cfg.read_bytes() == before

    r = client.post(
        f"/devices/{NEW_MAC}/settings",
        json={"allowlisted": True},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 200, r.text
    assert NEW_MAC in load_config_from_path(cfg).allowlist

    # CSRF: a body a cross-site <form> could emit without a preflight is refused on
    # its content type, not merely on whether it happens to parse as JSON.
    before = cfg.read_bytes()
    r = client.post(
        f"/devices/{NEW_MAC}/settings",
        content='{"allowlisted": false}',
        headers={"Content-Type": "text/plain", "Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 415
    assert cfg.read_bytes() == before


def test_ac_7_malformed_mac_rejected_valid_unknown_mac_accepted(tmp_path: Path) -> None:
    cfg = write_device_sample(tmp_path)
    before = cfg.read_bytes()
    client = device_client(tmp_path, cfg)

    for bad in ("not-a-mac", "aa:bb:cc:dd:ee", "zz:bb:cc:dd:ee:ff", "aabbccddeeff"):
        r = client.post(f"/devices/{bad}/settings", json={"allowlisted": True})
        assert r.status_code == 400, f"{bad} should be rejected"
        assert cfg.read_bytes() == before

    # Rejected *before* any file access — with no config on disk at all it is still a
    # clean 400, not a read error.
    missing = tmp_path / "nowhere" / "config.yaml"
    r = device_client(tmp_path, missing).post(
        "/devices/not-a-mac/settings", json={"allowlisted": True}
    )
    assert r.status_code == 400

    # A well-formed MAC the daemon has never seen is still configurable.
    r = client.post(f"/devices/{NEW_MAC}/settings", json={"allowlisted": True})
    assert r.status_code == 200, r.text
    assert NEW_MAC in load_config_from_path(cfg).allowlist


def test_ac_8_override_name_survives_both_write_paths(tmp_path: Path) -> None:
    cfg = write_device_sample(tmp_path)
    client = device_client(tmp_path, cfg)

    # The cosmetic label is a first-class schema field now, so neither writer drops it.
    assert ss.field_by_path("overrides[].name") is not None

    # Regression: a full /settings round-trip used to silently delete `name:` from
    # every overrides[] entry (config.py filters unknown keys; the UI only emitted
    # leaves that had a FieldSpec, and list overlays replace wholesale).
    r = client.post("/settings", json=payload_from(cfg))
    assert r.status_code == 200, r.text
    assert "leonardo s22" in cfg.read_text()

    # And the device card can set it.
    r = client.post(
        f"/devices/{OVERRIDE_MAC}/settings", json={"overrides": {"name": "garage wled"}}
    )
    assert r.status_code == 200, r.text
    assert "garage wled" in cfg.read_text()
    # Still parses — the daemon ignores the label (config.py:900 filters it out).
    assert load_config_from_path(cfg).overrides[0].mac == OVERRIDE_MAC


def test_ac_9_devices_row_toggle_posts_to_the_route_and_reflects_state(
    tmp_path: Path, seeded_db: Path
) -> None:
    from fastapi.testclient import TestClient

    from tests.ui.conftest import MAC_A, MAC_B
    from wifi_shepard_ui.app import create_app

    cfg = write_device_sample(tmp_path)
    client = TestClient(create_app(db_path=seeded_db, config_path=cfg))

    # Read views still work with no token configured.
    r = client.get("/devices")
    assert r.status_code == 200
    page = r.text

    # Every row carries a toggle aimed at the per-device route.
    for mac in (MAC_A, MAC_B):
        assert f'data-allowlist-toggle="{mac.lower()}"' in page.lower()

    # MAC_A is allowlisted in the sample config; MAC_B is not.
    assert MAC_B.lower() not in client.get("/devices?allowlist=yes").text.lower()

    r = client.post(f"/devices/{MAC_B}/settings", json={"allowlisted": True})
    assert r.status_code == 200, r.text

    # Reflected on reload, from config.yaml — no separate UI state.
    assert MAC_B.lower() in client.get("/devices?allowlist=yes").text.lower()
    assert MAC_B.lower() in load_config_from_path(cfg).allowlist
