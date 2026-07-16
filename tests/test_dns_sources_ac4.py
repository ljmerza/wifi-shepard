"""ADR-0011 AC-4: the merged source combines queries from two instances, and a down
instance degrades gracefully — it logs a warning and contributes nothing, so one dead
Pi-hole never blinds the other."""

from __future__ import annotations

import logging

from tests.conftest import FakeDnsSource
from wifi_shepard.dns_sources import DnsQuery, MergedDnsSource

_Q1 = DnsQuery(ts=1.0, client_ip="10.0.0.5", domain="a.com")
_Q2 = DnsQuery(ts=2.0, client_ip="10.0.0.6", domain="b.com")


async def test_merges_queries_from_both_instances():
    merged = MergedDnsSource([FakeDnsSource(queries=[_Q1]), FakeDnsSource(queries=[_Q2])])
    out = await merged.queries_since(0.0)
    assert set(out) == {_Q1, _Q2}


async def test_down_instance_degrades_gracefully(caplog):
    up = FakeDnsSource(queries=[_Q1])
    down = FakeDnsSource(fail=True)
    merged = MergedDnsSource([up, down])

    with caplog.at_level(logging.WARNING, logger="wifi_shepard.dns_sources"):
        out = await merged.queries_since(0.0)

    assert out == [_Q1], "the healthy instance's queries must still be used"
    assert any(r.message == "dns_source_unavailable" for r in caplog.records)


async def test_login_tolerates_a_down_instance(caplog):
    class FailingLogin:
        name = "dead-pihole"

        async def login(self) -> None:
            raise RuntimeError("connection refused")

        async def queries_since(self, since: float):
            return []

        async def close(self) -> None:
            return None

    healthy = FakeDnsSource()
    merged = MergedDnsSource([healthy, FailingLogin()])

    with caplog.at_level(logging.WARNING, logger="wifi_shepard.dns_sources"):
        await merged.login()  # must not raise

    assert healthy.login_calls == 1, "the healthy instance must still be logged in"
    assert any(r.message == "dns_source_login_failed" for r in caplog.records)


async def test_close_fans_out_to_all_instances():
    a = FakeDnsSource()
    b = FakeDnsSource()
    await MergedDnsSource([a, b]).close()
    assert a.closed and b.closed
