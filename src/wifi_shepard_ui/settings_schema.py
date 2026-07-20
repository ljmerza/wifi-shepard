"""Declarative settings schema for the wifi-shepard config surface (ADR-0013).

One :class:`FieldSpec` per editable leaf field in ``wifi_shepard.config.Config``,
carrying the metadata the settings UI needs to render, validate, and serialize a
field — plus a *plain-English* description written for someone who does not know
WiFi internals. This module is the single source of truth for what the UI can
edit and how each knob is explained; ``tests/ui/test_settings_schema_coverage_ac1``
asserts every field reachable from ``Config`` is covered here (or explicitly
excluded), so a future config field can't silently become un-editable (AC-1).

Kept dependency-free (no FastAPI/Jinja/YAML) so the daemon-side coverage test and
the UI can both import it cheaply.

``restart_required`` marks fields consumed only at daemon startup — controller /
Home-Assistant / DNS-source wiring, and the reboot on/off toggles that create or
destroy the scheduler task (``main.py`` builds those once; a reload only retunes
thresholds/scanner/backoff/quiet-hours/overrides/allowlist). Everything else takes
effect on the next scan cycle after a save (ADR-0013 AC-7).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    """How the UI renders/parses a field. ``str``-valued so templates can compare
    against the plain string."""

    INT = "int"
    # ADR-0009 disable-able criterion: an integer, or "disabled" (YAML null) to
    # turn the signal off entirely.
    INT_OR_NULL = "int_or_null"
    BOOL = "bool"
    ENUM = "enum"
    STRING = "string"
    # A secret that is NEVER typed in the UI — the operator names the env var that
    # holds it, and the YAML stores a ${NAME} placeholder resolved only in the
    # daemon's environment (ADR-0013 §Decision).
    SECRET_REF = "secret_ref"
    MAC = "mac"
    TIME_HHMM = "time_hhmm"
    TIMEZONE = "timezone"
    INT_LIST = "int_list"
    STRING_LIST = "string_list"
    MAC_LIST = "mac_list"


@dataclass(frozen=True)
class FieldSpec:
    path: str  # canonical config path, e.g. "detection.signal_dbm_max" or "overrides[].mac"
    label: str
    kind: Kind
    description: str
    section: str
    default: object = None
    unit: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] | None = None
    secret: bool = False
    restart_required: bool = False
    # For INT_OR_NULL fields: when the operator leaves it blank, does that write an
    # explicit YAML null (disable the check — the 3 detection criteria, ADR-0009) or
    # omit the key entirely (inherit/keep the normal value — quiet_hours, overrides,
    # controllers.port)? True = write null; False = omit.
    blank_writes_null: bool = False
    # Where this field lives in the YAML, when that differs from `path` (which tracks
    # the Config *dataclass* shape). The quiet_hours override thresholds are flat on
    # QuietHoursConfig but nested under `quiet_hours.override_threshold:` in the YAML.
    # None = same as `path`. config_io reads/writes yaml_path; the form key stays `path`.
    yaml_path: str | None = None


@dataclass(frozen=True)
class SectionSpec:
    key: str
    label: str
    help: str


# Section-level help. The detection help carries the two rules a newcomer most
# needs: the conjunctive-AND model and the "at least one criterion on" rule.
SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec(
        key="detection",
        label="Detection — what counts as a 'misbehaving' device",
        help=(
            "A device is flagged only when it fails ALL of the enabled checks below at the "
            "same time, on every reading in the window — weak signal AND slow AND retrying AND "
            "on a busy access point. This 'must fail everything' rule is deliberate: it stops "
            "healthy battery-saving gadgets from being nudged for no reason, but it also means a "
            "strong-signal device is never flagged by these no matter how badly it behaves. Set "
            "any single check to 'disabled' to drop it from the group; at least one must stay on. "
            "The silent-device and DNS-loop detectors further down are SEPARATE and do not take "
            "part in this AND."
        ),
    ),
    SectionSpec(
        key="scanner",
        label="Scanner — how often it looks, and the master safety switch",
        help=(
            "How frequently the daemon inspects the network, and 'dry run' — the master switch "
            "that lets it watch and log without actually touching anything."
        ),
    ),
    SectionSpec(
        key="backoff",
        label="Backoff — escalating patience per device",
        help=(
            "The more often the SAME device has to be nudged, the longer the daemon waits before "
            "nudging it again, up to hard hourly/daily ceilings — so a stubborn device isn't "
            "hammered, and a hopeless one is eventually left alone (quarantined)."
        ),
    ),
    SectionSpec(
        key="safety_rails",
        label="Safety rails — network-wide brakes",
        help=(
            "Limits that apply across ALL devices so a bad moment doesn't cause a storm of "
            "disconnects. Both are off (0) by default. Recommended: turn them on with dry-run for "
            "a week, watch the 'deferred' log lines, then size them before going live."
        ),
    ),
    SectionSpec(
        key="quiet_hours",
        label="Quiet hours — be stricter overnight",
        help=(
            "An optional daily window in which the daemon demands stricter thresholds before it "
            "nudges anything — so a marginal problem doesn't cause disruption at a bad time. Leave "
            "the whole section off to disable."
        ),
    ),
    SectionSpec(
        key="reboot",
        label="Reboot — power-cycle a hopeless device (via Home Assistant)",
        help=(
            "Last resort: instead of only disconnecting a device that won't behave, actually "
            "power-cycle it through Home Assistant (its restart button or a smart-plug outlet). "
            "Opt-in and off by default; only devices you explicitly list under 'eligible' are ever "
            "rebooted, and never an allowlisted one."
        ),
    ),
    SectionSpec(
        key="overrides",
        label="Per-device overrides",
        help=(
            "Tuning for individual devices. Anything set here replaces the global value FOR THAT "
            "ONE DEVICE (per-device always wins). Use it to go easier on a device you know is "
            "fine, or stricter on a known troublemaker."
        ),
    ),
    SectionSpec(
        key="allowlist",
        label="Allowlist — never touch these",
        help=(
            "Devices the daemon must never nudge or reboot under any circumstance — your phones, "
            "laptops, anything you don't want it interfering with. Match the MAC exactly, in "
            "lowercase."
        ),
    ),
    SectionSpec(
        key="controllers",
        label="Controllers — how it logs in to your WiFi",
        help=(
            "Your WiFi controller(s) — how the daemon signs in to see devices and move them. "
            "Changing anything here takes effect after a daemon restart."
        ),
    ),
    SectionSpec(
        key="home_assistant",
        label="Home Assistant — optional notifications",
        help=(
            "Optional. Sends you a phone notification (via Home Assistant) whenever the daemon "
            "nudges or quarantines a device. Leave off to run silently. Changes take effect after "
            "a restart."
        ),
    ),
    SectionSpec(
        key="dns_sources",
        label="DNS sources — Pi-hole (for the reconnect-loop detector)",
        help=(
            "Optional Pi-hole connection(s) that feed the DNS reconnect-loop detector above. List "
            "every Pi-hole a device might use for name lookups; their data is merged. Changes take "
            "effect after a restart."
        ),
    ),
)


# Fields deliberately NOT editable from the UI, each with a reason. Empty today —
# the whole Config surface is editable — but the mechanism exists so a future
# internal-only field can be excluded explicitly rather than silently missed (AC-1).
EXCLUDED_PATHS: frozenset[str] = frozenset()

# The converse of EXCLUDED_PATHS (ADR-0014): fields that live in config.yaml and are
# editable here, but have NO counterpart in the daemon's `Config` dataclasses because
# the loader ignores them (`config.py` filters unknown override keys). They are labels
# for humans. Listing them explicitly is what stops them being silently dropped on a
# save — the bug that deleted `overrides[].name` from every entry before ADR-0014.
COSMETIC_PATHS: frozenset[str] = frozenset({"overrides[].name"})


@dataclass(frozen=True)
class OptionalSectionSpec:
    """A config block whose mere *presence* in the YAML enables it (no `enabled:` field —
    an absent block means the feature is off). The UI gives each an explicit enable
    toggle so a round-trip save never activates a block just because its fields carry
    non-empty defaults (e.g. quiet_hours start/end), and never writes `detection.dns_thrash`
    (which requires a dns_sources block) unless the operator asked for it.
    """

    path: str  # dotted config path, e.g. "quiet_hours" or "detection.dns_thrash"
    ui_section: str  # SECTIONS key the enable toggle renders under
    label: str


OPTIONAL_SECTIONS: tuple[OptionalSectionSpec, ...] = (
    OptionalSectionSpec("detection.dns_thrash", "detection", "Enable DNS reconnect-loop detection"),
    OptionalSectionSpec("quiet_hours", "quiet_hours", "Enable quiet hours"),
    OptionalSectionSpec("home_assistant", "home_assistant", "Enable Home Assistant notifications"),
    OptionalSectionSpec("dns_sources", "dns_sources", "Enable DNS (Pi-hole) sources"),
)


def optional_sections_for(ui_section: str) -> tuple[OptionalSectionSpec, ...]:
    return tuple(o for o in OPTIONAL_SECTIONS if o.ui_section == ui_section)


@dataclass(frozen=True)
class MembershipSpec:
    """A MAC-list field that the *device* pages render as a single on/off toggle
    (ADR-0014). On the Settings page the same field is a list of MACs; on a device
    page the only question is whether this one MAC is in it.
    """

    key: str  # per-device payload key, e.g. "allowlisted"
    path: str  # the MAC_LIST FieldSpec path, e.g. "allowlist"
    label: str  # toggle text, phrased for one device rather than a list


# The three per-MAC memberships, in the order the device card renders them.
PER_DEVICE_MEMBERSHIPS: tuple[MembershipSpec, ...] = (
    MembershipSpec("allowlisted", "allowlist", "Never nudge this device"),
    MembershipSpec(
        "inactivity_watched",
        "detection.inactivity.macs",
        "Watch this device for a wedged session",
    ),
    MembershipSpec("reboot_eligible", "reboot.eligible", "Allow power-cycling this device"),
)

# The per-MAC object lists, as (per-device payload key, item_prefix) pairs. The
# matching ObjectListSpec supplies where each lands in the config mapping, so the
# location is never written down twice.
PER_DEVICE_OBJECT_LISTS: tuple[tuple[str, str], ...] = (
    ("overrides", "overrides[]."),
    ("reboot_override", "reboot.overrides[]."),
)


_KICK_MECHANISMS = ("deauth", "btm", "auto")

FIELDS: tuple[FieldSpec, ...] = (
    # ----------------------------------------------------------------- detection
    FieldSpec(
        path="detection.tx_rate_kbps_max",
        label="Max data rate to count as 'slow'",
        kind=Kind.INT_OR_NULL,
        section="detection",
        default=12000,
        unit="kbps",
        minimum=0,
        blank_writes_null=True,
        description=(
            "How slow a device's WiFi link speed has to be to look 'bad'. This is the raw radio "
            "rate between the device and the access point — NOT your internet speed. 12000 kbps "
            "(12 Mbps) is quite slow for modern WiFi; a healthy phone usually reports 100,000+. "
            "Only devices transmitting SLOWER than this count toward being flagged. Lower it to be "
            "pickier, raise it to catch more, or set it to 'disabled' to ignore data rate entirely."
        ),
    ),
    FieldSpec(
        path="detection.retry_pct_max",
        label="Max retry rate to count as 'struggling'",
        kind=Kind.INT_OR_NULL,
        section="detection",
        default=30,
        unit="%",
        minimum=0,
        maximum=100,
        blank_writes_null=True,
        description=(
            "How often a device has to re-send WiFi frames before it looks 'bad'. Every wireless "
            "link retries a few frames; a healthy device stays well under 10%. At 30%, nearly a "
            "third of its transmissions fail on the first try — a sign it's struggling with its "
            "current access point. Only devices retrying MORE than this count. Set to 'disabled' "
            "to ignore retries."
        ),
    ),
    FieldSpec(
        path="detection.signal_dbm_max",
        label="Weakest signal to count as 'far/weak'",
        kind=Kind.INT_OR_NULL,
        section="detection",
        default=-70,
        unit="dBm",
        minimum=-100,
        maximum=0,
        blank_writes_null=True,
        description=(
            "How weak a device's signal must be to look 'bad'. Signal is measured in dBm and is "
            "always negative — closer to 0 is stronger. Roughly: -50 is right next to the access "
            "point, -70 is a couple of rooms away, -80 is weak and far. Only devices WEAKER (more "
            "negative) than this are eligible to be nudged. Make it more negative (e.g. -75) to "
            "act only on genuinely distant devices, or less negative (e.g. -65) to act sooner. "
            "Set to 'disabled' to ignore signal strength."
        ),
    ),
    FieldSpec(
        path="detection.ap_cu_total_min",
        label="Only act when the AP is at least this busy",
        kind=Kind.INT,
        section="detection",
        default=0,
        unit="%",
        minimum=0,
        maximum=100,
        description=(
            "Only nudge a device when its access point is actually congested. 'Channel "
            "utilization' is how crowded the airwaves are on that AP, from 0 to 100%. At 0 (off) "
            "the daemon acts regardless of how busy the AP is. Set it to e.g. 60 so a struggling "
            "device is only moved when its AP is at least 60% busy — moving a device off a quiet "
            "AP gains nothing. Higher = only act on very congested APs."
        ),
    ),
    FieldSpec(
        path="detection.radios",
        label="Which bands to watch",
        kind=Kind.STRING_LIST,
        section="detection",
        default=("ng",),
        description=(
            "Which WiFi bands the checks apply to. 'ng' = 2.4 GHz (the crowded band cheap IoT "
            "clings to), 'na' = 5 GHz, '6e' = 6 GHz. This tool exists to free up 2.4 GHz airtime, "
            "so the default is just 'ng'. Add bands only if you want the same checks there too."
        ),
    ),
    # --------------------------------------------------- detection.inactivity
    FieldSpec(
        path="detection.inactivity.enabled",
        label="Enable the silent-device detector",
        kind=Kind.BOOL,
        section="detection",
        default=False,
        description=(
            "A SEPARATE detector (not part of the AND above): flags a device you've listed that "
            "has a strong connection but has gone completely silent — no data in or out for a "
            "while — which usually means its app/session has wedged even though WiFi looks fine. "
            "Off by default; you opt in device-by-device below."
        ),
    ),
    FieldSpec(
        path="detection.inactivity.min_bytes_per_window",
        label="Traffic floor that counts as 'silent'",
        kind=Kind.INT,
        section="detection",
        default=1024,
        unit="bytes",
        minimum=0,
        description=(
            "How little traffic — sent plus received, added together — over the window counts as "
            "'silent'. 1024 bytes is essentially nothing (a truly wedged device). Raise it if a "
            "device that dribbles tiny keep-alive packets should still count as silent. Note this "
            "counts LAN traffic too, so it approximates 'no activity', not strictly 'no internet'."
        ),
    ),
    FieldSpec(
        path="detection.inactivity.window_samples",
        label="How many silent polls in a row",
        kind=Kind.INT,
        section="detection",
        default=30,
        minimum=1,
        description=(
            "How many polls in a row a device must stay under the traffic floor before it's "
            "flagged. At a 60-second poll interval, 30 is about 30 minutes of continuous silence. "
            "Higher = more patient (fewer false alarms); lower = quicker to act."
        ),
    ),
    FieldSpec(
        path="detection.inactivity.macs",
        label="Devices the silent-detector watches",
        kind=Kind.MAC_LIST,
        section="detection",
        default=(),
        description=(
            "The devices this detector watches, by MAC address. It ONLY ever looks at devices you "
            "list here — nothing is watched automatically. Leaving this empty makes the detector "
            "do nothing even when it's enabled."
        ),
    ),
    # -------------------------------------------------- detection.dns_thrash
    FieldSpec(
        path="detection.dns_thrash.same_domain_queries_max",
        label="Max lookups of the same address per window",
        kind=Kind.INT,
        section="detection",
        default=20,
        minimum=0,
        description=(
            "Optional DNS reconnect-loop detector (needs a Pi-hole source below). Catches a device "
            "stuck asking for the SAME web address over and over — a wedged smart device can look "
            "up its server hundreds of times an hour. This is how many lookups of one address "
            "within the window are allowed before it looks stuck. Chatty-but-normal devices can "
            "legitimately hit 20-50, so this often needs RAISING to avoid false alarms. Only "
            "lookups over this count."
        ),
    ),
    FieldSpec(
        path="detection.dns_thrash.window_minutes",
        label="Counting window",
        kind=Kind.INT,
        section="detection",
        default=60,
        unit="minutes",
        minimum=1,
        description=(
            "The length of the counting window, in minutes. Lookups of each address are tallied "
            "over this rolling window."
        ),
    ),
    FieldSpec(
        path="detection.dns_thrash.sustain_windows",
        label="Windows in a row before flagging",
        kind=Kind.INT,
        section="detection",
        default=2,
        minimum=1,
        description=(
            "How many windows in a row the device must stay over the limit before it's flagged. "
            "For example 2 windows of 60 minutes means the behavior must persist for 2 hours — "
            "this avoids acting on a brief burst."
        ),
    ),
    # ------------------------------------------------------------------- scanner
    FieldSpec(
        path="scanner.poll_interval_seconds",
        label="How often to check the network",
        kind=Kind.INT,
        section="scanner",
        default=60,
        unit="seconds",
        minimum=1,
        description=(
            "How often, in seconds, the daemon inspects every device. 60 is a good default. "
            "Shorter reacts faster but polls the controller harder; longer is gentler but slower "
            "to notice problems."
        ),
    ),
    FieldSpec(
        path="scanner.window_samples",
        label="Consecutive bad readings before acting",
        kind=Kind.INT,
        section="scanner",
        default=5,
        minimum=1,
        description=(
            "How many of the most recent polls must ALL look bad before a device is acted on. At a "
            "60-second interval, 5 is about 5 minutes of continuous bad behavior. This is the main "
            "'don't overreact to a blip' control — higher = more patient."
        ),
    ),
    FieldSpec(
        path="scanner.dry_run",
        label="Dry run (master safety switch)",
        kind=Kind.BOOL,
        section="scanner",
        default=True,
        description=(
            "The master safety switch. When ON, the daemon does everything EXCEPT actually nudge "
            "devices — it just logs what it WOULD do, so you can watch it for a week before "
            "trusting it. Turn it OFF to let it act for real. There is no per-device dry run; this "
            "is global."
        ),
    ),
    FieldSpec(
        path="scanner.kick_mechanism",
        label="How to nudge a device",
        kind=Kind.ENUM,
        section="scanner",
        default="deauth",
        choices=_KICK_MECHANISMS,
        description=(
            "How a device is nudged to reconnect (hopefully to a better access point). 'deauth' = "
            "a forced disconnect (works everywhere, a bit blunt). 'btm' = a polite 802.11v 'please "
            "move' request (only some devices obey it). 'auto' = try the polite request first, "
            "fall back to a forced disconnect if it doesn't move. Individual devices can override "
            "this below."
        ),
    ),
    # ------------------------------------------------------------------- backoff
    FieldSpec(
        path="backoff.cooldowns_seconds",
        label="Waiting ladder between repeat nudges",
        kind=Kind.INT_LIST,
        section="backoff",
        default=(300, 1800, 7200, 43200, 86400),
        unit="seconds",
        description=(
            "The escalating wait, in seconds, applied per device based on how many times in a row "
            "it's been nudged. For example 300, 1800, 7200 means wait 5 minutes after the 1st "
            "nudge, 30 minutes after the 2nd, 2 hours after the 3rd, and so on. Once a device "
            "reaches the last rung it stays there. Empty = no cooldown."
        ),
    ),
    FieldSpec(
        path="backoff.max_kicks_per_hour",
        label="Hard cap: nudges per device per hour",
        kind=Kind.INT,
        section="backoff",
        default=0,
        minimum=0,
        description=(
            "A hard ceiling on how many times a single device can be nudged in any rolling hour. "
            "0 = no limit. A backstop against a device that keeps re-triggering."
        ),
    ),
    FieldSpec(
        path="backoff.max_kicks_per_day",
        label="Hard cap: nudges per device per day",
        kind=Kind.INT,
        section="backoff",
        default=0,
        minimum=0,
        description="Same idea over a rolling 24 hours. 0 = no limit.",
    ),
    FieldSpec(
        path="backoff.quarantine_after_kicks",
        label="Give up (quarantine) after this many nudges",
        kind=Kind.INT,
        section="backoff",
        default=5,
        minimum=0,
        description=(
            "After this many nudges to one device, stop nudging it and 'quarantine' it — just flag "
            "it instead — so a hopeless device doesn't churn forever. It's a signal that the "
            "device itself, or where it's placed, needs a look."
        ),
    ),
    # -------------------------------------------------------------- safety_rails
    FieldSpec(
        path="safety_rails.min_seconds_between_kicks",
        label="Minimum gap between ANY two nudges",
        kind=Kind.INT,
        section="safety_rails",
        default=0,
        unit="seconds",
        minimum=0,
        description=(
            "The minimum time between any two nudges anywhere on the network. Stops a flood of "
            "simultaneous disconnects (which can trip a controller's anti-abuse lockout) when many "
            "devices go bad at once. 0 = off."
        ),
    ),
    FieldSpec(
        path="safety_rails.max_kicks_per_ap_per_window",
        label="Max nudges against one AP per window",
        kind=Kind.INT,
        section="safety_rails",
        default=0,
        minimum=0,
        description=(
            "The most nudges allowed against a single access point within the window below. "
            "Prevents draining one AP when several of its devices misbehave together. 0 = off."
        ),
    ),
    FieldSpec(
        path="safety_rails.per_ap_window_seconds",
        label="Window for the per-AP cap",
        kind=Kind.INT,
        section="safety_rails",
        default=600,
        unit="seconds",
        minimum=0,
        description=(
            "The rolling window for the per-AP cap above, in seconds. 600 = 10 minutes. Must be "
            "greater than 0 when the per-AP cap is turned on."
        ),
    ),
    # --------------------------------------------------------------- quiet_hours
    FieldSpec(
        path="quiet_hours.start",
        label="Quiet-hours start",
        kind=Kind.TIME_HHMM,
        section="quiet_hours",
        default="23:00",
        description="When the stricter window begins, as 24-hour HH:MM in the timezone below.",
    ),
    FieldSpec(
        path="quiet_hours.end",
        label="Quiet-hours end",
        kind=Kind.TIME_HHMM,
        section="quiet_hours",
        default="07:00",
        description=(
            "When the stricter window ends, as 24-hour HH:MM. It may wrap past midnight (for "
            "example 23:00 to 07:00)."
        ),
    ),
    FieldSpec(
        path="quiet_hours.timezone",
        label="Timezone",
        kind=Kind.TIMEZONE,
        section="quiet_hours",
        default="America/Chicago",
        description=(
            "The timezone the start/end times are given in, as an IANA name like 'America/Chicago'."
        ),
    ),
    FieldSpec(
        path="quiet_hours.tx_rate_kbps_max",
        yaml_path="quiet_hours.override_threshold.tx_rate_kbps_max",
        label="Overnight data-rate threshold",
        kind=Kind.INT_OR_NULL,
        section="quiet_hours",
        default=None,
        unit="kbps",
        minimum=0,
        description=(
            "During quiet hours, use THIS data-rate threshold instead of the normal one (whichever "
            "is stricter wins). Leave unset to keep the normal value overnight."
        ),
    ),
    FieldSpec(
        path="quiet_hours.retry_pct_max",
        yaml_path="quiet_hours.override_threshold.retry_pct_max",
        label="Overnight retry threshold",
        kind=Kind.INT_OR_NULL,
        section="quiet_hours",
        default=None,
        unit="%",
        minimum=0,
        maximum=100,
        description=(
            "The retry threshold to use during quiet hours (stricter wins). Unset = keep the "
            "normal value overnight."
        ),
    ),
    FieldSpec(
        path="quiet_hours.signal_dbm_max",
        yaml_path="quiet_hours.override_threshold.signal_dbm_max",
        label="Overnight signal threshold",
        kind=Kind.INT_OR_NULL,
        section="quiet_hours",
        default=None,
        unit="dBm",
        minimum=-100,
        maximum=0,
        description=(
            "The signal threshold to use during quiet hours (stricter wins). Unset = keep the "
            "normal value overnight."
        ),
    ),
    # -------------------------------------------------------------------- reboot
    FieldSpec(
        path="reboot.enabled",
        label="Enable reboots",
        kind=Kind.BOOL,
        section="reboot",
        default=False,
        restart_required=True,
        description=(
            "Turn the reboot feature on or off. Turning it on or off takes effect after a daemon "
            "restart."
        ),
    ),
    FieldSpec(
        path="reboot.resolver",
        label="How reboots are resolved",
        kind=Kind.ENUM,
        section="reboot",
        default="home_assistant",
        choices=("home_assistant",),
        restart_required=True,
        description=(
            "Who figures out HOW to reboot a device. Only 'home_assistant' is supported — Home "
            "Assistant maps the device to its restart button or a smart-plug outlet."
        ),
    ),
    FieldSpec(
        path="reboot.dry_run",
        label="Reboot dry run",
        kind=Kind.BOOL,
        section="reboot",
        default=True,
        description=(
            "Like the master safety switch, but for reboots: when ON, log what WOULD be rebooted "
            "without actually doing it."
        ),
    ),
    FieldSpec(
        path="reboot.eligible",
        label="Devices allowed to be rebooted",
        kind=Kind.MAC_LIST,
        section="reboot",
        default=(),
        description=(
            "The ONLY devices allowed to be rebooted, by MAC. Nothing is ever rebooted unless it's "
            "on this list. An allowlisted device is never rebooted, even if you list it here."
        ),
    ),
    FieldSpec(
        path="reboot.overrides[].mac",
        label="Device MAC",
        kind=Kind.MAC,
        section="reboot",
        description="The device this explicit reboot target applies to.",
    ),
    FieldSpec(
        path="reboot.overrides[].name",
        label="Friendly name",
        kind=Kind.STRING,
        section="reboot",
        default=None,
        description="An optional label for the device, for your own reference.",
    ),
    FieldSpec(
        path="reboot.overrides[].ha_entity",
        label="Home Assistant switch/button",
        kind=Kind.STRING,
        section="reboot",
        default=None,
        description=(
            "The Home Assistant switch or button entity to toggle to reboot this device, for "
            "example 'switch.kitchen_plug'. Only needed for devices Home Assistant can't resolve "
            "on its own."
        ),
    ),
    FieldSpec(
        path="reboot.cooldown.per_device_seconds",
        label="Minimum time between reboots of one device",
        kind=Kind.INT,
        section="reboot",
        default=3600,
        unit="seconds",
        minimum=0,
        description=(
            "Minimum time between reboots of the SAME device, in seconds. 3600 = at most once an "
            "hour. 0 = off."
        ),
    ),
    FieldSpec(
        path="reboot.cooldown.max_per_device_per_day",
        label="Max reboots per device per day",
        kind=Kind.INT,
        section="reboot",
        default=4,
        minimum=0,
        description="The most times one device can be rebooted in a rolling 24 hours.",
    ),
    FieldSpec(
        path="reboot.proactive.enabled",
        label="Scheduled reboots",
        kind=Kind.BOOL,
        section="reboot",
        default=False,
        restart_required=True,
        description=(
            "Reboot eligible devices on a fixed daily schedule, whether or not they're currently "
            "misbehaving. Turning it on or off takes effect after a restart."
        ),
    ),
    FieldSpec(
        path="reboot.proactive.schedule",
        label="Daily reboot time",
        kind=Kind.TIME_HHMM,
        section="reboot",
        default="03:30",
        description="The daily time to run scheduled reboots, as 24-hour HH:MM local time.",
    ),
    FieldSpec(
        path="reboot.reactive.enabled",
        label="Reactive reboots (reserved)",
        kind=Kind.BOOL,
        section="reboot",
        default=False,
        description=(
            "Reserved for a future version: reboot a device when active reachability probes show "
            "it's unreachable. Not active yet — editing these reactive settings has no effect in "
            "this version."
        ),
    ),
    FieldSpec(
        path="reboot.reactive.probe.method",
        label="Probe method (reserved)",
        kind=Kind.ENUM,
        section="reboot",
        default="ping",
        choices=("ping", "http"),
        description="Reserved: how to probe a device's reachability. Not active yet.",
    ),
    FieldSpec(
        path="reboot.reactive.probe.interval_seconds",
        label="Probe interval (reserved)",
        kind=Kind.INT,
        section="reboot",
        default=60,
        unit="seconds",
        minimum=0,
        description="Reserved: seconds between reachability probes. Not active yet.",
    ),
    FieldSpec(
        path="reboot.reactive.probe.window_samples",
        label="Probe window (reserved)",
        kind=Kind.INT,
        section="reboot",
        default=5,
        minimum=0,
        description="Reserved: how many probes form the packet-loss window. Not active yet.",
    ),
    FieldSpec(
        path="reboot.reactive.probe.loss_pct_min",
        label="Loss % that means unreachable (reserved)",
        kind=Kind.INT,
        section="reboot",
        default=30,
        unit="%",
        minimum=0,
        maximum=100,
        description="Reserved: packet-loss percentage that counts as unreachable. Not active yet.",
    ),
    FieldSpec(
        path="reboot.reactive.require_signal_adequate",
        label="Require adequate signal (reserved)",
        kind=Kind.BOOL,
        section="reboot",
        default=True,
        description=(
            "Reserved: only reactively reboot if the device's signal is otherwise fine, so you "
            "don't reboot something that's merely far away. Not active yet."
        ),
    ),
    FieldSpec(
        path="reboot.reactive.after_failed_kicks",
        label="Failed nudges before escalating (reserved)",
        kind=Kind.INT,
        section="reboot",
        default=2,
        minimum=0,
        description=(
            "Reserved: how many failed nudges before escalating to a reactive reboot. Not active "
            "yet."
        ),
    ),
    # ----------------------------------------------------------------- overrides
    FieldSpec(
        path="overrides[].mac",
        label="Device MAC",
        kind=Kind.MAC,
        section="overrides",
        description="The device these per-device settings apply to, by MAC address.",
    ),
    FieldSpec(
        path="overrides[].name",
        label="Label",
        kind=Kind.STRING,
        section="overrides",
        description=(
            "A name for your own benefit, so you can tell which device this row is — "
            "'kitchen wled', 'back bedroom camera'. The daemon ignores it entirely; it only "
            "exists to keep the config readable."
        ),
    ),
    FieldSpec(
        path="overrides[].tx_rate_kbps_max",
        label="This device's data-rate threshold",
        kind=Kind.INT_OR_NULL,
        section="overrides",
        default=None,
        unit="kbps",
        minimum=0,
        description=(
            "This device's own data-rate threshold, replacing the global one. 'disabled' turns "
            "the rate check off for just this device; unset = use the global value."
        ),
    ),
    FieldSpec(
        path="overrides[].retry_pct_max",
        label="This device's retry threshold",
        kind=Kind.INT_OR_NULL,
        section="overrides",
        default=None,
        unit="%",
        minimum=0,
        maximum=100,
        description=(
            "This device's own retry threshold. 'disabled' turns the retry check off for this "
            "device; unset = use the global value."
        ),
    ),
    FieldSpec(
        path="overrides[].signal_dbm_max",
        label="This device's signal threshold",
        kind=Kind.INT_OR_NULL,
        section="overrides",
        default=None,
        unit="dBm",
        minimum=-100,
        maximum=0,
        description=(
            "This device's own signal threshold. 'disabled' turns the signal check off for this "
            "device; unset = use the global value."
        ),
    ),
    FieldSpec(
        path="overrides[].ap_cu_total_min",
        label="This device's AP-busy floor",
        kind=Kind.INT_OR_NULL,
        section="overrides",
        default=None,
        unit="%",
        minimum=0,
        maximum=100,
        description=(
            "This device's own 'only act when its AP is at least this busy' floor. Unset = use the "
            "global value."
        ),
    ),
    FieldSpec(
        path="overrides[].kick_mechanism",
        label="This device's nudge method",
        kind=Kind.ENUM,
        section="overrides",
        default=None,
        choices=_KICK_MECHANISMS,
        description="How to nudge THIS device specifically. Unset = use the global mechanism.",
    ),
    FieldSpec(
        path="overrides[].max_kicks_per_hour",
        label="This device's hourly nudge cap",
        kind=Kind.INT_OR_NULL,
        section="overrides",
        default=None,
        minimum=0,
        description="This device's own hourly nudge cap. Unset = use the global cap.",
    ),
    FieldSpec(
        path="overrides[].max_kicks_per_day",
        label="This device's daily nudge cap",
        kind=Kind.INT_OR_NULL,
        section="overrides",
        default=None,
        minimum=0,
        description="This device's own daily nudge cap. Unset = use the global cap.",
    ),
    FieldSpec(
        path="overrides[].dns_same_domain_queries_max",
        label="This device's DNS-lookup limit",
        kind=Kind.INT_OR_NULL,
        section="overrides",
        default=None,
        minimum=0,
        description=(
            "This device's own same-address DNS-lookup limit for the reconnect-loop detector. "
            "Unset = use the global value."
        ),
    ),
    # ----------------------------------------------------------------- allowlist
    FieldSpec(
        path="allowlist",
        label="Never-touch devices",
        kind=Kind.MAC_LIST,
        section="allowlist",
        default=(),
        description=(
            "Devices that are NEVER nudged or rebooted, by MAC. Match exactly, in lowercase, to "
            "match how the controller reports them."
        ),
    ),
    # --------------------------------------------------------------- controllers
    FieldSpec(
        path="controllers[].type",
        label="Controller type",
        kind=Kind.ENUM,
        section="controllers",
        default="unifi",
        choices=("unifi",),
        restart_required=True,
        description="The controller brand. Only 'unifi' is supported today.",
    ),
    FieldSpec(
        path="controllers[].name",
        label="Name",
        kind=Kind.STRING,
        section="controllers",
        restart_required=True,
        description="A label for this controller — your choice.",
    ),
    FieldSpec(
        path="controllers[].host",
        label="Address (IP or hostname)",
        kind=Kind.STRING,
        section="controllers",
        restart_required=True,
        description="The controller's address, for example 192.168.1.1.",
    ),
    FieldSpec(
        path="controllers[].username",
        label="Username",
        kind=Kind.STRING,
        section="controllers",
        restart_required=True,
        description=(
            "The login username for the controller. Best practice is a dedicated limited account, "
            "not your main admin login."
        ),
    ),
    FieldSpec(
        path="controllers[].password",
        label="Password (env var name)",
        kind=Kind.SECRET_REF,
        section="controllers",
        secret=True,
        restart_required=True,
        description=(
            "The controller password. For safety you do NOT type it here — instead name the "
            "environment variable that holds it (for example UNIFI_PASSWORD). The secret stays in "
            "your env file and is only read by the daemon."
        ),
    ),
    FieldSpec(
        path="controllers[].site",
        label="Site",
        kind=Kind.STRING,
        section="controllers",
        default="default",
        restart_required=True,
        description="The controller 'site' name. Most setups have one site called 'default'.",
    ),
    FieldSpec(
        path="controllers[].verify_ssl",
        label="Verify HTTPS certificate",
        kind=Kind.BOOL,
        section="controllers",
        default=True,
        restart_required=True,
        description=(
            "Whether to strictly check the controller's HTTPS certificate. UniFi gateways ship a "
            "self-signed certificate that won't validate, so those setups usually set this to "
            "false. Turn it back on once you install a real certificate."
        ),
    ),
    FieldSpec(
        path="controllers[].port",
        label="API port",
        kind=Kind.INT_OR_NULL,
        section="controllers",
        default=None,
        minimum=1,
        maximum=65535,
        restart_required=True,
        description=(
            "The controller's API port. Leave unset to use the default (8443 for a standalone "
            "controller). UDM-class gateways (UDM, UDM Pro, Cloud Gateway, Dream Router) use 443."
        ),
    ),
    # ------------------------------------------------------------ home_assistant
    FieldSpec(
        path="home_assistant.url",
        label="Home Assistant address",
        kind=Kind.STRING,
        section="home_assistant",
        restart_required=True,
        description="Your Home Assistant address, for example http://homeassistant:8123.",
    ),
    FieldSpec(
        path="home_assistant.token",
        label="Access token (env var name)",
        kind=Kind.SECRET_REF,
        section="home_assistant",
        secret=True,
        restart_required=True,
        description=(
            "The Home Assistant long-lived access token. You do NOT type it here — name the "
            "environment variable that holds it (for example HA_TOKEN)."
        ),
    ),
    FieldSpec(
        path="home_assistant.notify_service",
        label="Notify service",
        kind=Kind.STRING,
        section="home_assistant",
        restart_required=True,
        description=(
            "Which Home Assistant notify service to send to, for example 'mobile_app_pixel' for "
            "the app on your phone."
        ),
    ),
    # --------------------------------------------------------------- dns_sources
    FieldSpec(
        path="dns_sources[].type",
        label="Source type",
        kind=Kind.ENUM,
        section="dns_sources",
        default="pihole",
        choices=("pihole",),
        restart_required=True,
        description="The DNS source type. Only 'pihole' (Pi-hole v6) is supported.",
    ),
    FieldSpec(
        path="dns_sources[].password",
        label="Shared password default (env var name)",
        kind=Kind.SECRET_REF,
        section="dns_sources",
        secret=True,
        restart_required=True,
        description=(
            "Optional. A default admin/API password used by any Pi-hole below that doesn't set its "
            "own. You do NOT type it here — name the environment variable that holds it (for "
            "example PIHOLE_PASSWORD). Leave blank if every Pi-hole has its own password."
        ),
    ),
    FieldSpec(
        path="dns_sources[].instances[].url",
        label="Pi-hole address",
        kind=Kind.STRING,
        section="dns_sources",
        restart_required=True,
        description=(
            "One Pi-hole's address, for example http://192.168.1.186. Add one entry per Pi-hole a "
            "device might use for lookups."
        ),
    ),
    FieldSpec(
        path="dns_sources[].instances[].password",
        label="This Pi-hole's password (env var name)",
        kind=Kind.SECRET_REF,
        section="dns_sources",
        secret=True,
        restart_required=True,
        description=(
            "This Pi-hole's own admin/API password, when it differs from the others. You do NOT "
            "type it here — name the environment variable that holds it (for example "
            "PIHOLE_GYM_PASSWORD). Leave blank to use the shared default above."
        ),
    ),
)


@dataclass(frozen=True)
class ObjectListSpec:
    """A repeatable list-of-objects section (per-MAC overrides, controllers, etc.)."""

    key: str  # payload/DOM key, e.g. "reboot.overrides"
    section: str  # which SECTIONS box it renders under
    item_prefix: str  # leaf FieldSpec prefix, e.g. "reboot.overrides[]."
    location: tuple[str, ...]  # where it lands in the config mapping, e.g. ("reboot","overrides")
    add_label: str  # button text
    nested_key: str | None = None  # e.g. "instances"
    nested_prefix: str | None = None  # e.g. "dns_sources[].instances[]."
    nested_add_label: str | None = None


OBJECT_LISTS: tuple[ObjectListSpec, ...] = (
    ObjectListSpec(
        "controllers", "controllers", "controllers[].", ("controllers",), "Add controller"
    ),
    ObjectListSpec("overrides", "overrides", "overrides[].", ("overrides",), "Add device override"),
    ObjectListSpec(
        "reboot.overrides",
        "reboot",
        "reboot.overrides[].",
        ("reboot", "overrides"),
        "Add reboot target",
    ),
    ObjectListSpec(
        "dns_sources",
        "dns_sources",
        "dns_sources[].",
        ("dns_sources",),
        "Add Pi-hole source",
        nested_key="instances",
        nested_prefix="dns_sources[].instances[].",
        nested_add_label="Add Pi-hole address",
    ),
)


def item_fields(prefix: str) -> tuple[FieldSpec, ...]:
    """Leaf FieldSpecs directly under an object-list item prefix (no deeper ``[]``)."""
    return tuple(
        f for f in FIELDS if f.path.startswith(prefix) and "[]" not in f.path[len(prefix) :]
    )


def object_lists_for_section(section: str) -> tuple[ObjectListSpec, ...]:
    return tuple(o for o in OBJECT_LISTS if o.section == section)


def object_list_by_prefix(prefix: str) -> ObjectListSpec | None:
    """The ObjectListSpec whose items carry ``prefix`` — the bridge from a per-device
    payload key to where that list lives in the config mapping."""
    for o in OBJECT_LISTS:
        if o.item_prefix == prefix:
            return o
    return None


def scalar_fields_for_section(section: str) -> tuple[FieldSpec, ...]:
    """Non-list scalar fields rendered directly in a section box."""
    list_kinds = (Kind.INT_LIST, Kind.STRING_LIST, Kind.MAC_LIST)
    return tuple(
        f
        for f in FIELDS
        if f.section == section and "[]" not in f.path and f.kind not in list_kinds
    )


def scalar_list_fields_for_section(section: str) -> tuple[FieldSpec, ...]:
    list_kinds = (Kind.INT_LIST, Kind.STRING_LIST, Kind.MAC_LIST)
    return tuple(
        f for f in FIELDS if f.section == section and "[]" not in f.path and f.kind in list_kinds
    )


def covered_paths() -> frozenset[str]:
    """Every config path the schema describes."""
    return frozenset(f.path for f in FIELDS)


def field_by_path(path: str) -> FieldSpec | None:
    for f in FIELDS:
        if f.path == path:
            return f
    return None


def fields_for_section(section: str) -> tuple[FieldSpec, ...]:
    return tuple(f for f in FIELDS if f.section == section)
