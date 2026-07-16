"""ADR-0011 AC-9: the per-MAC ``dns_same_domain_queries_max`` override resolves
override > global, consistent with resolve_caps."""

from __future__ import annotations

from tests.conftest import FakeDnsSource, make_client
from wifi_shepard.config import build_config
from wifi_shepard.dns_sources import DnsQuery
from wifi_shepard.dns_thrash import DnsThrashDetector
from wifi_shepard.resolution import resolve_dns_same_domain_max

_OVERRIDDEN = "aa:bb:cc:dd:ee:01"
_DEFAULT = "aa:bb:cc:dd:ee:02"
_O_IP = "10.0.0.5"
_D_IP = "10.0.0.6"
_DOMAIN = "mqtt-us-4.meross.com"
_T0 = 1_000_000.0


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _config():
    return build_config(
        dns_thrash={"same_domain_queries_max": 20, "window_minutes": 10, "sustain_windows": 2},
        dns_sources=[{"type": "pihole", "password": "pw", "instances": [{"url": "http://x"}]}],
        overrides=[{"mac": _OVERRIDDEN, "dns_same_domain_queries_max": 5}],
    )


def test_resolution_helper_is_override_over_global():
    config = _config()
    assert resolve_dns_same_domain_max(_OVERRIDDEN, config) == 5, "override wins"
    assert resolve_dns_same_domain_max(_DEFAULT, config) == 20, "global default otherwise"


async def test_override_lowers_the_effective_threshold():
    clock = Clock(_T0)
    src = FakeDnsSource()
    detector = DnsThrashDetector(_config(), src, now_fn=clock)
    clients = [make_client(mac=_OVERRIDDEN, ip=_O_IP), make_client(mac=_DEFAULT, ip=_D_IP)]

    # 10 queries/domain: over the override's 5 (for _OVERRIDDEN) but under the global 20.
    for step in (0, 600, 1200):
        clock.t = _T0 + step
        src.queries = [
            DnsQuery(ts=clock.t - i, client_ip=_O_IP, domain=_DOMAIN) for i in range(10)
        ] + [DnsQuery(ts=clock.t - i, client_ip=_D_IP, domain=_DOMAIN) for i in range(10)]
        flagged = await detector.observe(clients)

    assert flagged == [_OVERRIDDEN], (
        "only the MAC with the lower per-MAC override crosses its threshold; the other "
        "stays under the global default"
    )
