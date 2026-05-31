from __future__ import annotations

import dataclasses
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from wifi_shepard.reboot.oui import looks_like_espressif

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

# ADR-0006: reactive probe transport is a closed set; the proactive schedule is a
# 24h HH:MM local time. Both fail closed so a typo can't silently disable a guard.
_VALID_PROBE_METHODS: frozenset[str] = frozenset({"ping", "http"})
_SCHEDULE_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_MAC_PATTERN = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _is_valid_mac(value: Any) -> bool:
    return isinstance(value, str) and _MAC_PATTERN.match(value) is not None


def _require_non_negative_int(value: Any, key: str) -> int:
    # Reject bool explicitly: YAML parses yes/no into Python bool, an int subclass,
    # so a bare isinstance(int) check would silently accept it (ADR-0004 AC-7).
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a non-negative integer; got {value!r}")
    if value < 0:
        raise ValueError(f"{key} must be >= 0; got {value!r}")
    return value


def _require_bool(value: Any, key: str) -> bool:
    # Fail closed on a non-bool toggle: `enabled: "no"` (a quoted string) would
    # otherwise coerce truthy and silently arm a guard the operator meant to disable.
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean; got {value!r}")
    return value


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
    # ADR-0008: AP-saturation gate (PLAN.md §3). 0 = off (act regardless of AP
    # channel utilization); shipped configs set 60. Per-MAC overridable.
    ap_cu_total_min: int = 0
    radios: tuple[str, ...] = ("ng",)


@dataclass(frozen=True)
class ScannerConfig:
    poll_interval_seconds: int = 60
    window_samples: int = 5
    dry_run: bool = True
    kick_mechanism: str = "deauth"


@dataclass(frozen=True)
class BackoffConfig:
    # ADR-0007: per-MAC escalating backoff + hard caps. cooldowns_seconds is
    # indexed by the trailing run of recent kicks (clamped to the last entry);
    # the caps are rolling 1h / 24h windows. All three are opt-in (empty / 0 =
    # off), mirroring ADR-0004 safety_rails; config.example.yaml ships them on.
    quarantine_after_kicks: int = 5
    cooldowns_seconds: tuple[int, ...] = ()
    max_kicks_per_hour: int = 0
    max_kicks_per_day: int = 0


@dataclass(frozen=True)
class QuietHoursConfig:
    # ADR-0007: during [start, end) local time, a kick requires the *stricter*
    # override thresholds (per-field more-conservative-wins). Times are 24h
    # HH:MM; timezone is an IANA name. ap_cu_total_min is intentionally absent —
    # the §3 AP-saturation gate is a separate follow-up; the loader rejects it.
    start: str
    end: str
    timezone: str
    tx_rate_kbps_max: int | None = None
    retry_pct_max: int | None = None
    signal_dbm_max: int | None = None


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
    # ADR-0008: per-MAC AP-saturation floor (override > global). None = inherit.
    ap_cu_total_min: int | None = None
    kick_mechanism: str | None = None
    # ADR-0007: per-MAC kick-cap overrides (override > global). None = inherit.
    max_kicks_per_hour: int | None = None
    max_kicks_per_day: int | None = None


@dataclass(frozen=True)
class RebootOverride:
    # ADR-0005: explicit per-MAC reboot target for devices HA can't auto-resolve.
    # `name` is the human label (the slot config.example's overrides already drop).
    mac: str
    name: str | None = None
    ha_entity: str | None = None


@dataclass(frozen=True)
class RebootCooldownConfig:
    # ADR-0006: per-device reboot rate limits, reusing the ADR-0004 posture.
    # 0 = off for the single-flight cooldown; the daily cap is a rolling 24h window.
    per_device_seconds: int = 3600
    max_per_device_per_day: int = 4


@dataclass(frozen=True)
class RebootProactiveConfig:
    # ADR-0006 Phase 1: scheduled reboots of eligible MACs at a daily HH:MM local time.
    enabled: bool = False
    schedule: str = "03:30"


@dataclass(frozen=True)
class RebootProbeConfig:
    # ADR-0006 Phase 2 (reactive): active reachability probe. Schema only this PR.
    method: str = "ping"
    interval_seconds: int = 60
    window_samples: int = 5
    loss_pct_min: int = 30


@dataclass(frozen=True)
class RebootReactiveConfig:
    # ADR-0006 Phase 2+ (reactive escalation). Ships off; schema validated now so
    # the config contract is locked before the probe loop lands.
    enabled: bool = False
    probe: RebootProbeConfig = field(default_factory=RebootProbeConfig)
    require_signal_adequate: bool = True
    after_failed_kicks: int = 2


@dataclass(frozen=True)
class RebootConfig:
    # ADR-0005: opt-in, default-off. `eligible` lists MACs the operator allows
    # rebooting; HA resolves *how*. `overrides` are the explicit fallback targets.
    # ADR-0006 adds dry_run (log would_reboot only, like would_kick) plus the
    # cooldown / proactive / reactive sub-blocks.
    enabled: bool = False
    resolver: str = "home_assistant"
    dry_run: bool = True
    eligible: tuple[str, ...] = ()
    overrides: tuple[RebootOverride, ...] = ()
    cooldown: RebootCooldownConfig = field(default_factory=RebootCooldownConfig)
    proactive: RebootProactiveConfig = field(default_factory=RebootProactiveConfig)
    reactive: RebootReactiveConfig = field(default_factory=RebootReactiveConfig)


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
    quiet_hours: QuietHoursConfig | None = None
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

    dry_run = raw.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ValueError(f"reboot.dry_run must be a boolean; got {dry_run!r}")

    cooldown = _build_reboot_cooldown(raw.get("cooldown"))
    proactive = _build_reboot_proactive(raw.get("proactive"))
    reactive = _build_reboot_reactive(raw.get("reactive"))

    return RebootConfig(
        enabled=enabled,
        resolver=str(resolver),
        dry_run=dry_run,
        eligible=tuple(eligible),
        overrides=tuple(overrides),
        cooldown=cooldown,
        proactive=proactive,
        reactive=reactive,
    )


def _build_reboot_cooldown(raw: Mapping[str, Any] | None) -> RebootCooldownConfig:
    if raw is None:
        return RebootCooldownConfig()
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"reboot.cooldown must be a YAML mapping, got {type(raw).__name__}: {raw!r}"
        )
    return RebootCooldownConfig(
        per_device_seconds=_require_non_negative_int(
            raw.get("per_device_seconds", 3600), "reboot.cooldown.per_device_seconds"
        ),
        max_per_device_per_day=_require_non_negative_int(
            raw.get("max_per_device_per_day", 4), "reboot.cooldown.max_per_device_per_day"
        ),
    )


def _build_reboot_proactive(raw: Mapping[str, Any] | None) -> RebootProactiveConfig:
    if raw is None:
        return RebootProactiveConfig()
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"reboot.proactive must be a YAML mapping, got {type(raw).__name__}: {raw!r}"
        )
    schedule = raw.get("schedule", "03:30")
    if not isinstance(schedule, str) or _SCHEDULE_PATTERN.match(schedule) is None:
        raise ValueError(
            f"reboot.proactive.schedule must be a 24h HH:MM time (e.g. '03:30'); got {schedule!r}"
        )
    return RebootProactiveConfig(
        enabled=_require_bool(raw.get("enabled", False), "reboot.proactive.enabled"),
        schedule=schedule,
    )


def _build_reboot_reactive(raw: Mapping[str, Any] | None) -> RebootReactiveConfig:
    if raw is None:
        return RebootReactiveConfig()
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"reboot.reactive must be a YAML mapping, got {type(raw).__name__}: {raw!r}"
        )
    probe_raw = raw.get("probe")
    probe = RebootProbeConfig()
    if probe_raw is not None:
        if not isinstance(probe_raw, Mapping):
            raise ValueError(
                f"reboot.reactive.probe must be a YAML mapping, got "
                f"{type(probe_raw).__name__}: {probe_raw!r}"
            )
        method = probe_raw.get("method", "ping")
        if method not in _VALID_PROBE_METHODS:
            raise ValueError(
                f"reboot.reactive.probe.method must be one of "
                f"{sorted(_VALID_PROBE_METHODS)}; got {method!r}"
            )
        probe = RebootProbeConfig(
            method=str(method),
            interval_seconds=_require_non_negative_int(
                probe_raw.get("interval_seconds", 60), "reboot.reactive.probe.interval_seconds"
            ),
            window_samples=_require_non_negative_int(
                probe_raw.get("window_samples", 5), "reboot.reactive.probe.window_samples"
            ),
            loss_pct_min=_require_non_negative_int(
                probe_raw.get("loss_pct_min", 30), "reboot.reactive.probe.loss_pct_min"
            ),
        )
    return RebootReactiveConfig(
        enabled=_require_bool(raw.get("enabled", False), "reboot.reactive.enabled"),
        probe=probe,
        require_signal_adequate=_require_bool(
            raw.get("require_signal_adequate", True), "reboot.reactive.require_signal_adequate"
        ),
        after_failed_kicks=_require_non_negative_int(
            raw.get("after_failed_kicks", 2), "reboot.reactive.after_failed_kicks"
        ),
    )


_QUIET_HOURS_THRESHOLD_FIELDS: frozenset[str] = frozenset(
    {"tx_rate_kbps_max", "retry_pct_max", "signal_dbm_max"}
)


def _build_quiet_hours(raw: Mapping[str, Any] | None) -> QuietHoursConfig | None:
    """Parse + validate the quiet_hours: block (ADR-0007). None (no block) → disabled.

    Fail-closed: bad HH:MM, an unknown IANA timezone, or an unsupported
    override_threshold key (notably ap_cu_total_min — the §3 AP-saturation gate is
    not implemented yet) all raise at parse time rather than silently disable a
    guard the operator believes is active.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"quiet_hours must be a YAML mapping, got {type(raw).__name__}: {raw!r}")
    for key in ("start", "end", "timezone"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ValueError(f"quiet_hours.{key} is required and must be a non-empty string")
    for key in ("start", "end"):
        if _SCHEDULE_PATTERN.match(raw[key]) is None:
            raise ValueError(
                f"quiet_hours.{key} must be a 24h HH:MM time (e.g. '23:00'); got {raw[key]!r}"
            )
    try:
        ZoneInfo(raw["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"quiet_hours.timezone must be a valid IANA zone; got {raw['timezone']!r}"
        ) from exc

    override_raw = raw.get("override_threshold") or {}
    if not isinstance(override_raw, Mapping):
        raise ValueError(
            f"quiet_hours.override_threshold must be a YAML mapping, got "
            f"{type(override_raw).__name__}: {override_raw!r}"
        )
    thresholds: dict[str, int] = {}
    for key, value in override_raw.items():
        if key == "ap_cu_total_min":
            raise ValueError(
                "quiet_hours.override_threshold.ap_cu_total_min is not yet supported — the "
                "AP-saturation gate (PLAN.md §3 detection.ap_cu_total_min) is unimplemented and "
                "tracked in a follow-up ADR. Remove this key until then."
            )
        if key not in _QUIET_HOURS_THRESHOLD_FIELDS:
            raise ValueError(
                f"quiet_hours.override_threshold.{key} is not a recognized threshold; "
                f"supported: {sorted(_QUIET_HOURS_THRESHOLD_FIELDS)}"
            )
        # signal_dbm_max is negative; tx_rate/retry are non-negative. Reject
        # bool/non-int either way.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"quiet_hours.override_threshold.{key} must be an integer; got {value!r}"
            )
        if key != "signal_dbm_max" and value < 0:
            raise ValueError(f"quiet_hours.override_threshold.{key} must be >= 0; got {value!r}")
        thresholds[key] = value
    return QuietHoursConfig(
        start=raw["start"],
        end=raw["end"],
        timezone=raw["timezone"],
        tx_rate_kbps_max=thresholds.get("tx_rate_kbps_max"),
        retry_pct_max=thresholds.get("retry_pct_max"),
        signal_dbm_max=thresholds.get("signal_dbm_max"),
    )


def build_config(
    *,
    tx_rate_kbps_max: int = 12000,
    retry_pct_max: int = 30,
    signal_dbm_max: int = -70,
    ap_cu_total_min: int = 0,
    radios: tuple[str, ...] = ("ng",),
    dry_run: bool = True,
    window_samples: int = 5,
    poll_interval_seconds: int = 60,
    quarantine_after_kicks: int = 5,
    cooldowns_seconds: Sequence[int] = (),
    max_kicks_per_hour: int = 0,
    max_kicks_per_day: int = 0,
    kick_mechanism: str = "deauth",
    safety_rails: Mapping[str, Any] | None = None,
    quiet_hours: Mapping[str, Any] | None = None,
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
        ap_cu_total_min=_require_non_negative_int(ap_cu_total_min, "detection.ap_cu_total_min"),
        radios=tuple(radios),
    )
    scanner = ScannerConfig(
        poll_interval_seconds=poll_interval_seconds,
        window_samples=window_samples,
        dry_run=dry_run,
        kick_mechanism=kick_mechanism,
    )
    backoff = BackoffConfig(
        quarantine_after_kicks=quarantine_after_kicks,
        cooldowns_seconds=tuple(
            _require_non_negative_int(c, f"backoff.cooldowns_seconds[{i}]")
            for i, c in enumerate(cooldowns_seconds)
        ),
        max_kicks_per_hour=_require_non_negative_int(
            max_kicks_per_hour, "backoff.max_kicks_per_hour"
        ),
        max_kicks_per_day=_require_non_negative_int(max_kicks_per_day, "backoff.max_kicks_per_day"),
    )
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
        for cap_field in ("max_kicks_per_hour", "max_kicks_per_day"):
            cap_value = getattr(entry, cap_field)
            if cap_value is not None:
                _require_non_negative_int(cap_value, f"overrides[mac={entry.mac}].{cap_field}")
        if entry.ap_cu_total_min is not None:
            _require_non_negative_int(
                entry.ap_cu_total_min, f"overrides[mac={entry.mac}].ap_cu_total_min"
            )
    controllers_typed = tuple(_build_controller_spec(c, i) for i, c in enumerate(controllers))
    safety_rails_cfg = _build_safety_rails(safety_rails)
    quiet_hours_cfg = _build_quiet_hours(quiet_hours)
    reboot_cfg = _build_reboot(reboot)
    # Config-load advisories for the reboot: block (ADR-0005). MAC comparison uses
    # the same canonical form as reboot.normalize_mac (strip + lowercase).
    allowlist_norm = {str(m).strip().lower() for m in allowlist}
    eligible_norm = {m.strip().lower() for m in reboot_cfg.eligible}
    for mac in reboot_cfg.eligible:
        if mac.strip().lower() in allowlist_norm:
            # ADR-0005 AC-4: allowlist always wins, but a MAC in both surfaces is a
            # contradiction the operator should see at load time. No OUI warning is
            # emitted for it — the allowlist warning is the salient signal.
            logger.warning("reboot_eligible_in_allowlist", extra={"mac": mac})
        elif not looks_like_espressif(mac):
            # ADR-0005 Fork B: advisory OUI pre-filter. A non-Espressif OUI in
            # eligible is likely a typo onto a laptop/phone; the opt-in is still
            # honored, the warning just nudges the operator to double-check.
            logger.warning("reboot_eligible_non_espressif_oui", extra={"mac": mac})
    # An override target for a MAC never opted into `eligible` is dead config:
    # resolve_reboot_target gates on eligibility first, so the override never
    # fires. Surface it at load time (mirrors the allowlist∩eligible warning).
    for override in reboot_cfg.overrides:
        if override.mac.strip().lower() not in eligible_norm:
            logger.warning("reboot_override_mac_not_eligible", extra={"mac": override.mac})
    return Config(
        detection=detection,
        scanner=scanner,
        backoff=backoff,
        safety_rails=safety_rails_cfg,
        quiet_hours=quiet_hours_cfg,
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
        # Raw (no int() wrapper, unlike the siblings): build_config's
        # _require_non_negative_int fail-closes on a non-int/negative (ADR-0008 AC-6).
        ap_cu_total_min=detection_data.get("ap_cu_total_min", 0),
        radios=radios,
        quarantine_after_kicks=int(backoff_data.get("quarantine_after_kicks", 5)),
        cooldowns_seconds=_require_sequence(
            backoff_data.get("cooldowns_seconds"), "backoff.cooldowns_seconds"
        ),
        max_kicks_per_hour=backoff_data.get("max_kicks_per_hour", 0),
        max_kicks_per_day=backoff_data.get("max_kicks_per_day", 0),
        safety_rails=safety_rails_data,
        quiet_hours=data.get("quiet_hours"),
        reboot=reboot_data,
        allowlist=allowlist,
        overrides=overrides,
        controllers=controllers,
    )
