"""ADR-0011 AC-5: a MAC resolving the same domain more than the threshold within the
window, sustained continuously for the configured duration, is flagged; under-threshold,
not-yet-sustained, or spread-across-different-domains traffic is not."""

from __future__ import annotations

from tests.conftest import FakeDnsSource, make_client
from wifi_shepard.config import build_config
from wifi_shepard.dns_sources import DnsQuery
from wifi_shepard.dns_thrash import DnsThrashDetector

_MAC = "aa:bb:cc:dd:ee:01"
_IP = "10.0.0.5"
_DOMAIN = "mqtt-us-4.meross.com"
_T0 = 1_000_000.0


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _config(*, threshold: int = 20, window_minutes: int = 10, sustain_windows: int = 2):
    return build_config(
        dns_thrash={
            "same_domain_queries_max": threshold,
            "window_minutes": window_minutes,
            "sustain_windows": sustain_windows,
        },
        dns_sources=[{"type": "pihole", "password": "pw", "instances": [{"url": "http://x"}]}],
    )


def _same_domain(now: float, n: int, *, ip: str = _IP, domain: str = _DOMAIN):
    return [DnsQuery(ts=now - i, client_ip=ip, domain=domain) for i in range(n)]


async def test_over_threshold_and_sustained_is_flagged():
    clock = Clock(_T0)
    src = FakeDnsSource()
    detector = DnsThrashDetector(_config(), src, now_fn=clock)
    clients = [make_client(mac=_MAC, ip=_IP)]

    # sustain = 2 windows * 10 min = 1200s. Feed 25 (> 20) fresh queries each poll so
    # the trailing-window count stays over threshold continuously.
    clock.t = _T0
    src.queries = _same_domain(_T0, 25)
    assert await detector.observe(clients) == [], "over threshold but not yet sustained"

    for step in (300, 600, 900):
        clock.t = _T0 + step
        src.queries = _same_domain(clock.t, 25)
        assert await detector.observe(clients) == [], f"not sustained yet at +{step}s"

    clock.t = _T0 + 1200
    src.queries = _same_domain(clock.t, 25)
    assert await detector.observe(clients) == [_MAC], "sustained for the full duration -> flag"


async def test_under_threshold_is_never_flagged():
    clock = Clock(_T0)
    src = FakeDnsSource()
    detector = DnsThrashDetector(_config(), src, now_fn=clock)
    clients = [make_client(mac=_MAC, ip=_IP)]

    for step in (0, 600, 1200, 1800):
        clock.t = _T0 + step
        src.queries = _same_domain(clock.t, 5)  # 5 <= 20
        assert await detector.observe(clients) == []


async def test_traffic_spread_across_domains_is_not_flagged():
    clock = Clock(_T0)
    src = FakeDnsSource()
    detector = DnsThrashDetector(_config(), src, now_fn=clock)
    clients = [make_client(mac=_MAC, ip=_IP)]

    # 40 queries, but each to a distinct domain: no single domain exceeds 20.
    for step in (0, 600, 1200, 1800):
        clock.t = _T0 + step
        src.queries = [
            DnsQuery(ts=clock.t - i, client_ip=_IP, domain=f"host{i}.example.com")
            for i in range(40)
        ]
        assert await detector.observe(clients) == []


async def test_dropping_under_threshold_resets_the_sustain_streak():
    clock = Clock(_T0)
    src = FakeDnsSource()
    detector = DnsThrashDetector(_config(), src, now_fn=clock)
    clients = [make_client(mac=_MAC, ip=_IP)]

    # Over threshold for 900s (< 1200 sustain)...
    for step in (0, 300, 600, 900):
        clock.t = _T0 + step
        src.queries = _same_domain(clock.t, 25)
        assert await detector.observe(clients) == []

    # ...then after a gap larger than the window, a poll finds only 3 queries — under
    # threshold — which resets the streak marker.
    clock.t = _T0 + 1600
    src.queries = _same_domain(clock.t, 3)
    assert await detector.observe(clients) == []

    # Resuming over-threshold starts a fresh streak. Had the original streak continued,
    # +2500 (2500s > 1200 sustain) would flag; because it reset at +1600 it does not.
    clock.t = _T0 + 1900
    src.queries = _same_domain(clock.t, 25)
    assert await detector.observe(clients) == []
    clock.t = _T0 + 2500
    src.queries = _same_domain(clock.t, 25)
    assert await detector.observe(clients) == [], "streak restarted at +1900, not yet sustained"
