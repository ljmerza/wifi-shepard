from __future__ import annotations

import dataclasses
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("wifi_shepard.config")

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# ADR-0003 §Decision: kick_mechanism is a closed set. Anything else fails closed
# at config parse time so a typo (`kick_mechanism: dauth`) doesn't silently
# resolve to "deauth" and erase the operator's intent from the audit trail.
_VALID_KICK_MECHANISMS: frozenset[str] = frozenset({"deauth", "btm", "auto"})

# ADR-0005 §Decision: reboot identification is delegated to Home Assistant. The
# resolver name is a closed set so a typo fails closed instead of silently
# disabling reboot resolution.
_VALID_REBOOT_RESOLVERS: frozenset[str] = frozenset({"home_assistant"})

_MAC_PATTERN = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _is_valid_mac(value: Any) -> bool:
    return isinstance(value, str) and _MAC_PATTERN.match(value) is not None


def _interpolate_env(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(
                f"env var ${{{name}}} referenced in config but not set in the environment"
            )
        return os.environ[name]

    return _ENV_VAR_PATTERN.sub(repl, text)


def _walk_and_interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return _interpolate_env(value)
    if isinstance(value, Mapping):
        return {k: _walk_and_interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk_and_interpolate(item) for item in value]
    return value


def _require_sequence(value: Any, key: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{key} must be a YAML list, got {type(value).__name__}: {value!r}")
    return list(value)


def _require_mapping_items(items: list[Any], key: str) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(
                f"{key}[{i}] must be a YAML mapping, got {type(item).__name__}: {item!r}"
            )
        out.append(item)
    return out


@dataclass(frozen=True)
class DetectionConfig:
    tx_rate_kbps_max: int = 12000
    retry_pct_max: int = 30
    signal_dbm_max: int = -70
    radios: tuple[str, ...] = ("ng",)


@dataclass(frozen=True)
class ScannerConfig:
    poll_interval_seconds: int = 60
    window_samples: int = 5
    dry_run: bool = True
    kick_mechanism: str = "deauth"


@dataclass(frozen=True)
class BackoffConfig:
    quarantine_after_kicks: int = 5


@dataclass(frozen=True)
class SafetyRailsConfig:
    # ADR-0004: both limits opt-in. 0 = off.
    min_seconds_between_kicks: int = 0
    max_kicks_per_ap_per_window: int = 0
    per_ap_window_seconds: int = 600


@dataclass(frozen=True)
class OverrideEntry:
    mac: str
    tx_rate_kbps_max: int | None = None
    retry_pct_max: int | None = None
    signal_dbm_max: int | None = None
    kick_mechanism: str | None = None


@dataclass(frozen=True)
class RebootOverride:
    # ADR-0005: explicit per-MAC reboot target for devices HA can't auto-resolve.
    # `name` is the human label (the slot config.example's overrides already drop).
    mac: str
    name: str | None = None
    ha_entity: str | None = None


@dataclass(frozen=True)
class RebootConfig:
    # ADR-0005: opt-in, default-off. `eligible` lists MACs the operator allows
    # rebooting; HA resolves *how*. `overrides` are the explicit fallback targets.
    enabled: bool = False
    resolver: str = "home_assistant"
    eligible: tuple[str, ...] = ()
    overrides: tuple[RebootOverride, ...] = ()


@dataclass(frozen=True)
class ControllerSpec:
    type: str
    name: str
    host: str
    username: str
    password: str
    site: str = "default"
    verify_ssl: bool = True


@dataclass(frozen=True)
class Config:
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    backoff: BackoffConfig = field(default_factory=BackoffConfig)
    safety_rails: SafetyRailsConfig = field(default_factory=SafetyRailsConfig)
    reboot: RebootConfig = field(default_factory=RebootConfig)
    overrides: tuple[OverrideEntry, ...] = ()
    allowlist: tuple[str, ...] = ()
    controllers: tuple[ControllerSpec, ...] = ()


_CONTROLLER_REQUIRED = ("type", "name", "host", "username", "password")


def _build_controller_spec(item: Mapping[str, Any], index: int) -> ControllerSpec:
    for key in _CONTROLLER_REQUIRED:
        if key not in item or item[key] in (None, ""):
            raise ValueError(f"controllers[{index}].{key} is required")
    return ControllerSpec(
        type=str(item["type"]),
        name=str(item["name"]),
        host=str(item["host"]),
        username=str(item["username"]),
        password=str(item["password"]),
        site=str(item.get("site", "default")),
        verify_ssl=bool(item.get("verify_ssl", True)),
    )


def _build_safety_rails(raw: Mapping[str, Any] | None) -> SafetyRailsConfig:
    """Parse + validate the safety_rails: block. Fail-closed on ADR-0004 AC-7 inputs.

    Accepts None (no block in YAML) → defaults, both limits off.
    """
    if raw is None:
        return SafetyRailsConfig()
    fields: dict[str, int] = {}
    for key, default in (
        ("min_seconds_between_kicks", 0),
        ("max_kicks_per_ap_per_window", 0),
        ("per_ap_window_seconds", 600),
    ):
        value = raw.get(key, default)
        # Reject non-int (incl. bool, since bool is int-subclass — but only floats/strs
        # via YAML would normally appear, so we coerce int(value) and raise on TypeError
        # rather than tolerate floor()ing 0.5 silently).
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"safety_rails.{key} must be a non-negative integer; got {value!r} "
                f"({type(value).__name__})"
            )
        if value < 0:
            raise ValueError(f"safety_rails.{key} must be >= 0; got {value!r}")
        fields[key] = value
    if fields["max_kicks_per_ap_per_window"] > 0 and fields["per_ap_window_seconds"] == 0:
        raise ValueError(
            "safety_rails.per_ap_window_seconds must be > 0 when "
            "max_kicks_per_ap_per_window is set (otherwise every kick is "
            "immediately out-of-window and the cap never trips)"
        )
    return SafetyRailsConfig(**fields)


def _build_reboot(raw: Mapping[str, Any] | None) -> RebootConfig:
    """Parse + validate the reboot: block. Fail-closed on ADR-0005 AC-7 inputs.

    Accepts None (no block in YAML) → defaults, reboot disabled.
    """
    if raw is None:
        return RebootConfig()
    if not isinstance(raw, Mapping):
        raise ValueError(f"reboot must be a YAML mapping, got {type(raw).__name__}: {raw!r}")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"reboot.enabled must be a boolean; got {enabled!r}")

    resolver = raw.get("resolver", "home_assistant")
    if resolver not in _VALID_REBOOT_RESOLVERS:
        raise ValueError(
            f"reboot.resolver must be one of {sorted(_VALID_REBOOT_RESOLVERS)}; got {resolver!r}"
        )

    eligible_items = _require_sequence(raw.get("eligible"), "reboot.eligible")
    eligible: list[str] = []
    for i, mac in enumerate(eligible_items):
        if not _is_valid_mac(mac):
            raise ValueError(f"reboot.eligible[{i}] must be a MAC address string; got {mac!r}")
        eligible.append(mac)

    override_items = _require_mapping_items(
        _require_sequence(raw.get("overrides"), "reboot.overrides"), "reboot.overrides"
    )
    overrides: list[RebootOverride] = []
    for i, item in enumerate(override_items):
        mac = item.get("mac")
        if not _is_valid_mac(mac):
            raise ValueError(f"reboot.overrides[{i}].mac must be a MAC address string; got {mac!r}")
        ha_entity = item.get("ha_entity")
        if not ha_entity or not isinstance(ha_entity, str):
            raise ValueError(
                f"reboot.overrides[{i}] (mac={mac}) must declare a reboot target "
                f"(ha_entity); got {ha_entity!r}"
            )
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError(f"reboot.overrides[{i}].name must be a string when set; got {name!r}")
        overrides.append(RebootOverride(mac=str(mac), name=name, ha_entity=ha_entity))

    return RebootConfig(
        enabled=enabled,
        resolver=str(resolver),
        eligible=tuple(eligible),
        overrides=tuple(overrides),
    )


def build_config(
    *,
    tx_rate_kbps_max: int = 12000,
    retry_pct_max: int = 30,
    signal_dbm_max: int = -70,
    radios: tuple[str, ...] = ("ng",),
    dry_run: bool = True,
    window_samples: int = 5,
    poll_interval_seconds: int = 60,
    quarantine_after_kicks: int = 5,
    kick_mechanism: str = "deauth",
    safety_rails: Mapping[str, Any] | None = None,
    reboot: Mapping[str, Any] | None = None,
    overrides: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    allowlist: list[str] | tuple[str, ...] = (),
    controllers: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> Config:
    if kick_mechanism not in _VALID_KICK_MECHANISMS:
        raise ValueError(
            f"kick_mechanism must be one of {sorted(_VALID_KICK_MECHANISMS)}; "
            f"got {kick_mechanism!r}"
        )
    detection = DetectionConfig(
        tx_rate_kbps_max=tx_rate_kbps_max,
        retry_pct_max=retry_pct_max,
        signal_dbm_max=signal_dbm_max,
        radios=tuple(radios),
    )
    scanner = ScannerConfig(
        poll_interval_seconds=poll_interval_seconds,
        window_samples=window_samples,
        dry_run=dry_run,
        kick_mechanism=kick_mechanism,
    )
    backoff = BackoffConfig(quarantine_after_kicks=quarantine_after_kicks)
    known = {f.name for f in dataclasses.fields(OverrideEntry)}
    overrides_typed = tuple(
        OverrideEntry(**{k: v for k, v in o.items() if k in known}) for o in overrides
    )
    for entry in overrides_typed:
        if entry.kick_mechanism is not None and entry.kick_mechanism not in _VALID_KICK_MECHANISMS:
            raise ValueError(
                f"overrides[mac={entry.mac}].kick_mechanism must be one of "
                f"{sorted(_VALID_KICK_MECHANISMS)}; got {entry.kick_mechanism!r}"
            )
    controllers_typed = tuple(_build_controller_spec(c, i) for i, c in enumerate(controllers))
    safety_rails_cfg = _build_safety_rails(safety_rails)
    reboot_cfg = _build_reboot(reboot)
    # ADR-0005 AC-4: allowlist always wins, but a MAC in both surfaces is a
    # contradiction the operator should see at load time.
    allowlist_norm = {str(m).strip().lower() for m in allowlist}
    for mac in reboot_cfg.eligible:
        if mac.strip().lower() in allowlist_norm:
            logger.warning("reboot_eligible_in_allowlist", extra={"mac": mac})
    return Config(
        detection=detection,
        scanner=scanner,
        backoff=backoff,
        safety_rails=safety_rails_cfg,
        reboot=reboot_cfg,
        overrides=overrides_typed,
        allowlist=tuple(allowlist),
        controllers=controllers_typed,
    )


def load_config_from_path(path: Path | str) -> Config:
    text = Path(path).read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a YAML mapping, got {type(data).__name__}")

    data = _walk_and_interpolate(data)

    scanner_data = data.get("scanner") or {}
    detection_data = data.get("detection") or {}
    backoff_data = data.get("backoff") or {}
    safety_rails_data = data.get("safety_rails")  # None = no block → defaults
    reboot_data = data.get("reboot")  # None = no block → reboot disabled

    raw_dry_run = scanner_data.get("dry_run", True)
    if raw_dry_run is None:
        dry_run = True
    elif isinstance(raw_dry_run, bool):
        dry_run = raw_dry_run
    else:
        raise ValueError(
            f"scanner.dry_run must be a boolean, got {type(raw_dry_run).__name__}: {raw_dry_run!r}"
        )

    radios_raw = detection_data.get("radios")
    radios_list = _require_sequence(radios_raw, "detection.radios")
    radios = tuple(radios_list) if radios_list else ("ng",)
    allowlist = tuple(_require_sequence(data.get("allowlist"), "allowlist"))
    overrides = tuple(
        _require_mapping_items(_require_sequence(data.get("overrides"), "overrides"), "overrides")
    )
    controllers = tuple(
        _require_mapping_items(
            _require_sequence(data.get("controllers"), "controllers"), "controllers"
        )
    )

    return build_config(
        poll_interval_seconds=int(scanner_data.get("poll_interval_seconds", 60)),
        window_samples=int(scanner_data.get("window_samples", 5)),
        dry_run=dry_run,
        kick_mechanism=str(scanner_data.get("kick_mechanism", "deauth")),
        tx_rate_kbps_max=int(detection_data.get("tx_rate_kbps_max", 12000)),
        retry_pct_max=int(detection_data.get("retry_pct_max", 30)),
        signal_dbm_max=int(detection_data.get("signal_dbm_max", -70)),
        radios=radios,
        quarantine_after_kicks=int(backoff_data.get("quarantine_after_kicks", 5)),
        safety_rails=safety_rails_data,
        reboot=reboot_data,
        allowlist=allowlist,
        overrides=overrides,
        controllers=controllers,
    )
