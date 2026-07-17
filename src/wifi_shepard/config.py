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

from wifi_shepard.reboot import normalize_mac
from wifi_shepard.reboot.oui import looks_like_espressif

logger = logging.getLogger("wifi_shepard.config")

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_ENV_VAR_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
# Any ${...} reference, valid-form or not. Scanned *before* substitution so a
# lowercase or typo'd reference fails closed instead of passing through as the
# literal string (PR #3 issue #3); scanning the input also avoids false
# positives when an env var's substituted value itself contains "${".
_ENV_REF_PATTERN = re.compile(r"\$\{([^}]*)\}")

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

# ADR-0011: the DNS data-source backend is a closed set so an unknown `type:` fails
# closed at parse time rather than silently arming a detection with no data source.
_VALID_DNS_SOURCE_TYPES: frozenset[str] = frozenset({"pihole"})

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


def _optional_int_field(data: Mapping[str, Any], key: str, default: int) -> int | None:
    """ADR-0009 disable-able detection criterion. Absent key -> ``default`` (criterion
    active); explicit YAML ``null`` -> ``None`` (criterion disabled); otherwise coerce
    to ``int`` (rejecting bool, which YAML parses from yes/no as an int subclass)."""
    if key not in data:
        return default
    value = data[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"detection.{key} must be an integer or null; got {value!r}")
    return value


def _require_bool(value: Any, key: str) -> bool:
    # Fail closed on a non-bool toggle: `enabled: "no"` (a quoted string) would
    # otherwise coerce truthy and silently arm a guard the operator meant to disable.
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean; got {value!r}")
    return value


def _interpolate_env(text: str) -> str:
    for match in _ENV_REF_PATTERN.finditer(text):
        name = match.group(1)
        if _ENV_VAR_NAME.fullmatch(name) is None:
            raise ValueError(
                f"env reference ${{{name}}} in config is not an uppercase env var name "
                f"(expected ${{LIKE_THIS}}); refusing to pass it through literally"
            )

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


# Interpolation runs over the whole tree before any structural check, so a fragment
# that fails validation still holds the live UNIFI_PASSWORD / HA_TOKEN. The
# repr=False on ControllerSpec.password / HomeAssistantConfig.token cannot help: a
# rejected fragment never becomes a dataclass, and it is the raw dict that gets
# repr'd into the message — which reaches stderr at startup and container logs via
# main's config_reload_failed on SIGHUP. ADR-0001: those two are never logged.
_SECRET_KEYS: frozenset[str] = frozenset({"password", "token"})
_REDACTED = "***"


def _redact(value: Any) -> Any:
    """Copy of ``value`` with any secret-keyed entry masked, at any depth."""
    if isinstance(value, Mapping):
        return {
            k: _REDACTED if isinstance(k, str) and k.lower() in _SECRET_KEYS else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _describe(value: Any) -> str:
    """Render a rejected config fragment for an error message without leaking secrets.

    Containers keep their (redacted) contents — the key names are what make the message
    worth reading. A bare scalar is described by type alone: an interpolated
    ``${UNIFI_PASSWORD}`` that lands where a list or mapping was expected *is* the
    scalar, and there is no key to mask it by.
    """
    if isinstance(value, (Mapping, list)):
        return f"{type(value).__name__}: {_redact(value)!r}"
    return type(value).__name__


def _require_mac(value: Any, key: str) -> str:
    """Validate and canonicalize a MAC from config.

    Fails closed on a malformed entry. Storing it raw meant it simply never matched, so
    a typo in a safety-critical list silently protected nothing — the failure mode this
    is here to prevent.
    """
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a MAC address string; got {type(value).__name__}")
    mac = normalize_mac(value)
    if not _is_valid_mac(mac):
        raise ValueError(f"{key} must be a MAC address like aa:bb:cc:dd:ee:ff; got {value!r}")
    return mac


def _require_sequence(value: Any, key: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{key} must be a YAML list, got {_describe(value)}")
    return list(value)


def _require_mapping_items(items: list[Any], key: str) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"{key}[{i}] must be a YAML mapping, got {_describe(item)}")
        out.append(item)
    return out


@dataclass(frozen=True)
class InactivityConfig:
    # ADR-0010: "associated but no traffic" detector. An INDEPENDENT detection
    # class, not a fourth conjunctive client criterion — it flags an opted-in MAC
    # whose byte counters flatline over a window, catching a strong-signal client
    # whose application session is wedged (the failure the conjunctive scorer can
    # never see). Explicit per-MAC opt-in only (no baseline learning in v1);
    # `enabled: true` with an empty `macs` list is legal but inert.
    enabled: bool = False
    min_bytes_per_window: int = 1024
    window_samples: int = 30
    macs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DnsThrashConfig:
    # ADR-0011: DNS-thrash detection tunables. A MAC resolving one domain more than
    # `same_domain_queries_max` times within `window_minutes`, sustained continuously
    # for `sustain_windows * window_minutes`, is flagged. Absent block → feature off.
    same_domain_queries_max: int = 20
    window_minutes: int = 60
    sustain_windows: int = 2


@dataclass(frozen=True)
class DetectionConfig:
    # ADR-0009: each client criterion is disable-able. `None` (YAML `null`) turns
    # that signal off; omitting the key keeps the active default. At least one of
    # the three must stay enabled (validated in build_config).
    tx_rate_kbps_max: int | None = 12000
    retry_pct_max: int | None = 30
    signal_dbm_max: int | None = -70
    # ADR-0008: AP-saturation gate (PLAN.md §3). 0 = off (act regardless of AP
    # channel utilization); shipped configs set 60. Per-MAC overridable.
    ap_cu_total_min: int = 0
    radios: tuple[str, ...] = ("ng",)
    # ADR-0010: independent traffic-inactivity detector (opt-in, default-off).
    inactivity: InactivityConfig = field(default_factory=InactivityConfig)
    # ADR-0011: optional DNS-thrash detection. None (no `dns_thrash:` block) = off.
    dns_thrash: DnsThrashConfig | None = None


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
    # ADR-0011: per-MAC DNS-thrash threshold (override > global). None = inherit.
    dns_same_domain_queries_max: int | None = None


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
    # repr-suppressed so a logged/raised spec can't leak the secret (PR #3 issue #2).
    password: str = field(repr=False)
    site: str = "default"
    verify_ssl: bool = True
    # None = backend default (UniFi standalone: 8443). UDM-class gateways serve
    # the API on 443 (PR #3 issue #1).
    port: int | None = None


@dataclass(frozen=True)
class DnsInstanceSpec:
    # ADR-0011: one Pi-hole instance. Clients may use either of two resolvers, so a
    # source lists several instances and the merged source concatenates them.
    url: str


@dataclass(frozen=True)
class DnsSourceSpec:
    # ADR-0011: an optional DNS data source (Pi-hole v6 first). password is
    # repr-suppressed for the same reason as ControllerSpec.password.
    type: str
    password: str = field(repr=False)
    instances: tuple[DnsInstanceSpec, ...] = ()


@dataclass(frozen=True)
class HomeAssistantConfig:
    # PLAN.md §1/§4: per-kick / per-quarantine notifications via HA's REST
    # notify service. token is repr-suppressed for the same reason as
    # ControllerSpec.password.
    url: str
    token: str = field(repr=False)
    notify_service: str


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
    home_assistant: HomeAssistantConfig | None = None
    # ADR-0011: optional DNS data sources (empty = feature off).
    dns_sources: tuple[DnsSourceSpec, ...] = ()


_CONTROLLER_REQUIRED = ("type", "name", "host", "username", "password")


def _build_controller_spec(item: Mapping[str, Any], index: int) -> ControllerSpec:
    for key in _CONTROLLER_REQUIRED:
        if key not in item or item[key] in (None, ""):
            raise ValueError(f"controllers[{index}].{key} is required")
    port = item.get("port")
    if port is not None:
        # Reject bool explicitly: YAML yes/no parses to bool, an int subclass.
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError(f"controllers[{index}].port must be an integer; got {port!r}")
        if not 1 <= port <= 65535:
            raise ValueError(f"controllers[{index}].port must be in 1..65535; got {port!r}")
    return ControllerSpec(
        type=str(item["type"]),
        name=str(item["name"]),
        host=str(item["host"]),
        username=str(item["username"]),
        password=str(item["password"]),
        site=str(item.get("site", "default")),
        verify_ssl=bool(item.get("verify_ssl", True)),
        port=port,
    )


def _build_home_assistant(raw: Mapping[str, Any] | None) -> HomeAssistantConfig | None:
    """Parse + validate the home_assistant: block. None (no block) → notifications off.

    Fail-closed when the block is present: all three keys are required and
    non-empty so a missing ${HA_TOKEN} can't silently ship a notifier that
    401s on every kick.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"home_assistant must be a YAML mapping, got {_describe(raw)}")
    for key in ("url", "token", "notify_service"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"home_assistant.{key} is required and must be a non-empty string")
    url = raw["url"]
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"home_assistant.url must start with http:// or https://; got {url!r}")
    return HomeAssistantConfig(
        url=url,
        token=raw["token"],
        notify_service=raw["notify_service"],
    )


def _build_dns_thrash(raw: Mapping[str, Any] | None) -> DnsThrashConfig | None:
    """Parse + validate the detection.dns_thrash: block (ADR-0011). None (no block)
    → feature off. Fail-closed: all three knobs non-negative, and window/sustain must
    be >= 1 when the block is present (a zero-length window can never trip)."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"detection.dns_thrash must be a YAML mapping, got {_describe(raw)}")
    same_domain_queries_max = _require_non_negative_int(
        raw.get("same_domain_queries_max", 20), "detection.dns_thrash.same_domain_queries_max"
    )
    window_minutes = _require_non_negative_int(
        raw.get("window_minutes", 60), "detection.dns_thrash.window_minutes"
    )
    sustain_windows = _require_non_negative_int(
        raw.get("sustain_windows", 2), "detection.dns_thrash.sustain_windows"
    )
    if window_minutes < 1:
        raise ValueError("detection.dns_thrash.window_minutes must be >= 1")
    if sustain_windows < 1:
        raise ValueError("detection.dns_thrash.sustain_windows must be >= 1")
    return DnsThrashConfig(
        same_domain_queries_max=same_domain_queries_max,
        window_minutes=window_minutes,
        sustain_windows=sustain_windows,
    )


def _build_dns_sources(raw: Any) -> tuple[DnsSourceSpec, ...]:
    """Parse + validate the top-level dns_sources: list (ADR-0011). Fail-closed:
    unknown type, missing/empty password, or a bad instance url all raise at parse
    time. Absent / empty → () (feature off)."""
    items = _require_mapping_items(_require_sequence(raw, "dns_sources"), "dns_sources")
    specs: list[DnsSourceSpec] = []
    for i, item in enumerate(items):
        source_type = item.get("type")
        if source_type not in _VALID_DNS_SOURCE_TYPES:
            raise ValueError(
                f"dns_sources[{i}].type must be one of {sorted(_VALID_DNS_SOURCE_TYPES)}; "
                f"got {source_type!r}"
            )
        password = item.get("password")
        if not isinstance(password, str) or not password:
            raise ValueError(
                f"dns_sources[{i}].password is required and must be a non-empty string"
            )
        instance_items = _require_mapping_items(
            _require_sequence(item.get("instances"), f"dns_sources[{i}].instances"),
            f"dns_sources[{i}].instances",
        )
        if not instance_items:
            raise ValueError(f"dns_sources[{i}].instances must list at least one instance")
        instances: list[DnsInstanceSpec] = []
        for j, inst in enumerate(instance_items):
            url = inst.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError(
                    f"dns_sources[{i}].instances[{j}].url is required and must be non-empty"
                )
            if not url.startswith(("http://", "https://")):
                raise ValueError(
                    f"dns_sources[{i}].instances[{j}].url must start with http:// or https://; "
                    f"got {url!r}"
                )
            instances.append(DnsInstanceSpec(url=url))
        specs.append(
            DnsSourceSpec(
                type=str(source_type),
                password=password,
                instances=tuple(instances),
            )
        )
    return tuple(specs)


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
        raise ValueError(f"reboot must be a YAML mapping, got {_describe(raw)}")

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
        raise ValueError(f"reboot.cooldown must be a YAML mapping, got {_describe(raw)}")
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
        raise ValueError(f"reboot.proactive must be a YAML mapping, got {_describe(raw)}")
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
        raise ValueError(f"reboot.reactive must be a YAML mapping, got {_describe(raw)}")
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


def _build_inactivity(raw: Mapping[str, Any] | None) -> InactivityConfig:
    """Parse + validate the detection.inactivity: block (ADR-0010). None → defaults (off).

    Fail-closed in the config.py house style: a non-bool ``enabled``, a negative /
    non-int threshold, ``window_samples < 1`` while enabled, or a malformed MAC all
    raise at parse time. Each MAC is canonicalized (strip + lowercase) via
    ``_require_mac`` so the opt-in set matches the controller's inconsistent casing.
    ``enabled: true`` with an empty ``macs`` list is legal but inert.
    """
    if raw is None:
        return InactivityConfig()
    if not isinstance(raw, Mapping):
        raise ValueError(f"detection.inactivity must be a YAML mapping, got {_describe(raw)}")
    enabled = _require_bool(raw.get("enabled", False), "detection.inactivity.enabled")
    min_bytes_per_window = _require_non_negative_int(
        raw.get("min_bytes_per_window", 1024), "detection.inactivity.min_bytes_per_window"
    )
    window_samples = _require_non_negative_int(
        raw.get("window_samples", 30), "detection.inactivity.window_samples"
    )
    if enabled and window_samples < 1:
        raise ValueError(
            "detection.inactivity.window_samples must be >= 1 when inactivity detection is "
            f"enabled (an empty window can never accumulate a delta); got {window_samples!r}"
        )
    mac_items = _require_sequence(raw.get("macs"), "detection.inactivity.macs")
    macs = tuple(
        _require_mac(m, f"detection.inactivity.macs[{i}]") for i, m in enumerate(mac_items)
    )
    return InactivityConfig(
        enabled=enabled,
        min_bytes_per_window=min_bytes_per_window,
        window_samples=window_samples,
        macs=macs,
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
        raise ValueError(f"quiet_hours must be a YAML mapping, got {_describe(raw)}")
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
    tx_rate_kbps_max: int | None = 12000,
    retry_pct_max: int | None = 30,
    signal_dbm_max: int | None = -70,
    ap_cu_total_min: int = 0,
    radios: tuple[str, ...] = ("ng",),
    inactivity: Mapping[str, Any] | None = None,
    dns_thrash: Mapping[str, Any] | None = None,
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
    home_assistant: Mapping[str, Any] | None = None,
    dns_sources: Any = (),
) -> Config:
    if kick_mechanism not in _VALID_KICK_MECHANISMS:
        raise ValueError(
            f"kick_mechanism must be one of {sorted(_VALID_KICK_MECHANISMS)}; "
            f"got {kick_mechanism!r}"
        )
    dns_thrash_cfg = _build_dns_thrash(dns_thrash)
    detection = DetectionConfig(
        tx_rate_kbps_max=tx_rate_kbps_max,
        retry_pct_max=retry_pct_max,
        signal_dbm_max=signal_dbm_max,
        ap_cu_total_min=_require_non_negative_int(ap_cu_total_min, "detection.ap_cu_total_min"),
        radios=tuple(radios),
        inactivity=_build_inactivity(inactivity),
        dns_thrash=dns_thrash_cfg,
    )
    # ADR-0009: at least one client criterion must stay enabled — an all-null trio
    # would make every saturated client "bad" (the scorer fails safe, but reject it
    # here so the misconfig surfaces loudly rather than silently never/always acting).
    if (
        detection.tx_rate_kbps_max is None
        and detection.retry_pct_max is None
        and detection.signal_dbm_max is None
    ):
        raise ValueError(
            "detection must enable at least one client criterion; tx_rate_kbps_max, "
            "retry_pct_max, and signal_dbm_max are all null"
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
        if entry.dns_same_domain_queries_max is not None:
            _require_non_negative_int(
                entry.dns_same_domain_queries_max,
                f"overrides[mac={entry.mac}].dns_same_domain_queries_max",
            )
    controllers_typed = tuple(_build_controller_spec(c, i) for i, c in enumerate(controllers))
    home_assistant_cfg = _build_home_assistant(home_assistant)
    safety_rails_cfg = _build_safety_rails(safety_rails)
    quiet_hours_cfg = _build_quiet_hours(quiet_hours)
    reboot_cfg = _build_reboot(reboot)
    dns_sources_typed = _build_dns_sources(dns_sources)
    # ADR-0011: a dns_thrash: block the operator believes is armed must not silently
    # run with no data source — the UniFi controller cannot see per-client DNS.
    if dns_thrash_cfg is not None and not dns_sources_typed:
        raise ValueError(
            "detection.dns_thrash is configured but no dns_sources are defined; add a "
            "dns_sources: entry (e.g. a Pi-hole instance) or remove the dns_thrash block"
        )
    # The allowlist is the daemon's primary safety control, so it is canonicalized once
    # here and every consumer compares against that one form. It used to be stored raw
    # and matched exactly, which meant an uppercase entry (the form printed on device
    # labels) silently protected nothing.
    allowlist_typed = tuple(_require_mac(m, f"allowlist[{i}]") for i, m in enumerate(allowlist))
    # Config-load advisories for the reboot: block (ADR-0005). MAC comparison uses
    # the same canonical form as reboot.normalize_mac (strip + lowercase).
    allowlist_norm = set(allowlist_typed)
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
    # ADR-0010: an inactivity-opted-in MAC that is also allowlisted is a
    # contradiction the operator should see at load time. The allowlist wins
    # (the InactivityScorer skips allowlisted MACs regardless); this warning
    # mirrors reboot_eligible_in_allowlist. inactivity.macs are already
    # canonicalized, matching allowlist_norm's form.
    for mac in detection.inactivity.macs:
        if mac in allowlist_norm:
            logger.warning("inactivity_mac_in_allowlist", extra={"mac": mac})
    return Config(
        detection=detection,
        scanner=scanner,
        backoff=backoff,
        safety_rails=safety_rails_cfg,
        quiet_hours=quiet_hours_cfg,
        reboot=reboot_cfg,
        overrides=overrides_typed,
        allowlist=allowlist_typed,
        controllers=controllers_typed,
        home_assistant=home_assistant_cfg,
        dns_sources=dns_sources_typed,
    )


def load_config_from_path(path: Path | str) -> Config:
    text = Path(path).read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a YAML mapping, got {type(data).__name__}")

    data = _walk_and_interpolate(data)
    return build_config_from_mapping(data)


def build_config_from_mapping(data: Mapping[str, Any]) -> Config:
    """Validate + build a Config from an already-loaded mapping (no file read, no
    ``${VAR}`` interpolation). ``load_config_from_path`` calls this after reading and
    interpolating the file; the settings UI (ADR-0013) calls it directly on a proposed
    config whose secret fields still hold literal ``${NAME}`` placeholders — so the same
    fail-closed validation runs without the UI ever needing the real secret in its env.
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"config root must be a YAML mapping, got {type(data).__name__}")

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
        tx_rate_kbps_max=_optional_int_field(detection_data, "tx_rate_kbps_max", 12000),
        retry_pct_max=_optional_int_field(detection_data, "retry_pct_max", 30),
        signal_dbm_max=_optional_int_field(detection_data, "signal_dbm_max", -70),
        # Raw (no int() wrapper, unlike the siblings): build_config's
        # _require_non_negative_int fail-closes on a non-int/negative (ADR-0008 AC-6).
        ap_cu_total_min=detection_data.get("ap_cu_total_min", 0),
        radios=radios,
        # None (no block) → InactivityConfig defaults (off); _build_inactivity validates.
        inactivity=detection_data.get("inactivity"),
        # ADR-0011: None (no `dns_thrash:` key) → feature off. Passed raw so
        # _build_dns_thrash owns the fail-closed validation.
        dns_thrash=detection_data.get("dns_thrash"),
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
        home_assistant=data.get("home_assistant"),
        # ADR-0011: None/absent → () (feature off); _build_dns_sources validates.
        dns_sources=data.get("dns_sources"),
    )
