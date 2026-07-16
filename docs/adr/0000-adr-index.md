# Architecture Decision Records

## Status Definitions

| Status | Description |
|--------|-------------|
| Proposed | Under discussion, not yet accepted |
| Accepted | Decision accepted, not yet implemented |
| Partially Implemented | Accepted; some acceptance criteria shipped, others still open |
| Implemented | Decision accepted and fully implemented |
| Deprecated | No longer relevant |
| Superseded | Replaced by a newer ADR |

## ADR Index

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [0001](./0001-mvp-scope-base-feature.md) | MVP Scope for the Base Feature (dry_run-gated v1) | Partially Implemented | 2026-05-05 |
| [0002](./0002-device-history-and-status-ui.md) | UI for Device History & WiFi Status Overview | Implemented | 2026-05-08 |
| [0003](./0003-kick-mechanism-upgrade.md) | Kick Mechanism Upgrade — Speculative 802.11v BTM with One-Cycle Deauth Fallback | Implemented | 2026-05-09 |
| [0004](./0004-kick-rate-limits.md) | Kick Rate Limits — Global Single-Flight and Per-AP Cap | Implemented | 2026-05-11 |
| [0005](./0005-device-identification-and-reboot-backend.md) | Device Identification & Reboot-Backend Selection — Delegate to Home Assistant | Implemented | 2026-05-24 |
| [0006](./0006-reboot-remediation.md) | Reboot Remediation — Proactive Scheduling + Reactive Escalation | Partially Implemented | 2026-05-24 |
| [0007](./0007-action-policy-backoff-and-quiet-hours.md) | Complete the Action Policy — Per-MAC Backoff Schedule, Hard Caps, and Quiet Hours | Accepted | 2026-05-30 |
| [0008](./0008-ap-saturation-gate.md) | AP-Saturation Gate — Only Act on Saturated APs (detection.ap_cu_total_min) | Accepted | 2026-05-31 |
| [0009](./0009-disable-able-detection-criteria.md) | Disable-able Detection Criteria — `null` Turns a Client Signal Off | Implemented | 2026-06-14 |
| [0010](./0010-traffic-inactivity-detection.md) | Traffic-Inactivity Detection — an Opt-in "Associated but No Traffic" Flatline Detector | Accepted | 2026-07-15 |
| [0011](./0011-dns-thrash-detection-pihole.md) | DNS-Thrash Detection via an Optional Pi-hole Data Source | Accepted | 2026-07-15 |
