"""ADR-0011 AC-8: the MAC<->IP join comes from ClientSnapshot.ip (fail-soft). Clients
with an unknown IP are skipped without error, and queries from IPs with no matching
client are ignored."""

from __future__ import annotations

from tests.conftest import FakeDnsSource, make_client
from wifi_shepard.config import build_config
from wifi_shepard.dns_sources import DnsQuery
from wifi_shepard.dns_thrash import DnsThrashDetector

_M1 = "aa:bb:cc:dd:ee:01"
_M2 = "aa:bb:cc:dd:ee:02"
_M1_IP = "10.0.0.5"
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
    )


async def test_ip_none_client_skipped_and_unknown_ip_queries_ignored():
    clock = Clock(_T0)
    src = FakeDnsSource()
    detector = DnsThrashDetector(_config(), src, now_fn=clock)

    # M1 has an IP (joinable); M2 has ip=None (invisible to DNS, must not crash).
    clients = [make_client(mac=_M1, ip=_M1_IP), make_client(mac=_M2, ip=None)]

    for step in (0, 600, 1200):
        clock.t = _T0 + step
        src.queries = (
            # Over-threshold thrash for M1's IP.
            [DnsQuery(ts=clock.t - i, client_ip=_M1_IP, domain=_DOMAIN) for i in range(25)]
            # Over-threshold thrash from an IP with NO matching client — must be ignored.
            + [DnsQuery(ts=clock.t - i, client_ip="10.0.0.99", domain=_DOMAIN) for i in range(25)]
        )
        flagged = await detector.observe(clients)

    assert flagged == [_M1], (
        "only the IP-joined MAC is flagged; the ip=None client and the unknown-IP "
        "queries produce no flag and no error"
    )


async def test_no_error_when_all_clients_lack_ip():
    clock = Clock(_T0)
    src = FakeDnsSource(queries=[DnsQuery(ts=_T0, client_ip="10.0.0.5", domain=_DOMAIN)])
    detector = DnsThrashDetector(_config(), src, now_fn=clock)

    # Every client has ip=None: the join map is empty, every query is ignored, no crash.
    flagged = await detector.observe([make_client(mac=_M1, ip=None)])
    assert flagged == []
