"""DNS data-source factory (ADR-0011).

Mirrors ``controllers/__init__.build_controller``: turns the validated
``dns_sources:`` config into a single ``DnsSource`` (a ``MergedDnsSource`` composite,
even for one instance, so the lifecycle + degradation path is uniform). Returns
``None`` when nothing is configured (feature off). Unknown ``type:`` raises — config
validation already rejects it upstream, this is defense-in-depth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import DnsQuery, DnsSource
from .pihole import MergedDnsSource, PiholeSource

if TYPE_CHECKING:
    from ..config import DnsSourceSpec

__all__ = [
    "DnsQuery",
    "DnsSource",
    "MergedDnsSource",
    "PiholeSource",
    "build_dns_sources",
]


def build_dns_source_from_spec(spec: DnsSourceSpec) -> list[DnsSource]:
    """One config spec -> one ``DnsSource`` per instance (all sharing the spec's
    password). Unknown type fails closed."""
    if spec.type == "pihole":
        return [
            PiholeSource(url=inst.url, password=spec.password, name=f"pihole@{inst.url}")
            for inst in spec.instances
        ]
    raise ValueError(f"unknown dns source type: {spec.type!r}")


def build_dns_sources(config: Any) -> DnsSource | None:
    specs = config.dns_sources
    if not specs:
        return None
    sources: list[DnsSource] = []
    for spec in specs:
        sources.extend(build_dns_source_from_spec(spec))
    if not sources:
        return None
    return MergedDnsSource(sources)
