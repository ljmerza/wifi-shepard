"""ADR-0011 AC-3: PiholeSource authenticates against the v6 FTL REST API, fetches
queries with ``from=``, parses rows into DnsQuery, and re-auths exactly once on an
expired sid then succeeds.

Pi-hole v6 shapes (verified against the FTL OpenAPI spec):
    POST /api/auth {"password": ...} -> {"session": {"sid": ...}}
    GET  /api/queries?from=<unix>&length=N -> {"queries": [{time, client:{ip}, domain}]}
The sid is sent on the query request via the X-FTL-SID header.
"""

from __future__ import annotations

import re

from aioresponses import aioresponses
from yarl import URL

from wifi_shepard.dns_sources import DnsQuery, PiholeSource

_URL = "http://pi.hole"
_AUTH = f"{_URL}/api/auth"
_QUERIES_RE = re.compile(r"^http://pi\.hole/api/queries(\?.*)?$")

_ROWS = {
    "queries": [
        {"time": 1_700_000_000.5, "client": {"ip": "10.0.0.5", "name": "wled"}, "domain": "a.com"},
        {"time": 1_700_000_050.0, "client": {"ip": "10.0.0.6", "name": None}, "domain": "b.com"},
    ]
}


def _get_calls(mocked: aioresponses):
    out = []
    for (method, _url), calls in mocked.requests.items():
        if method == "GET":
            out.extend(calls)
    return out


async def test_authenticates_fetches_and_parses_queries():
    source = PiholeSource(url=_URL, password="pw")
    try:
        with aioresponses() as m:
            m.post(_AUTH, payload={"session": {"sid": "SID1", "valid": True}})
            m.get(_QUERIES_RE, payload=_ROWS)

            await source.login()
            queries = await source.queries_since(1_699_999_000.0)

        assert queries == [
            DnsQuery(ts=1_700_000_000.5, client_ip="10.0.0.5", domain="a.com"),
            DnsQuery(ts=1_700_000_050.0, client_ip="10.0.0.6", domain="b.com"),
        ]

        # The fetch carried `from=` (int-truncated) and the sid as X-FTL-SID.
        get_call = _get_calls(m)[0]
        assert get_call.kwargs["params"]["from"] == 1_699_999_000
        assert get_call.kwargs["headers"]["X-FTL-SID"] == "SID1"
    finally:
        await source.close()


async def test_reauthenticates_once_on_expired_sid_then_succeeds():
    source = PiholeSource(url=_URL, password="pw")
    try:
        with aioresponses() as m:
            m.post(_AUTH, payload={"session": {"sid": "SID1"}})  # login()
            m.get(_QUERIES_RE, status=401)  # expired sid
            m.post(_AUTH, payload={"session": {"sid": "SID2"}})  # re-auth
            m.get(_QUERIES_RE, payload=_ROWS)  # retry succeeds

            await source.login()
            queries = await source.queries_since(1_699_999_000.0)

        assert len(queries) == 2, "the retry after re-auth must return the parsed rows"

        # Exactly two auth POSTs (initial login + one re-auth) and two GET attempts.
        post_calls = m.requests[("POST", URL(_AUTH))]
        assert len(post_calls) == 2, "sid must be re-fetched exactly once on 401"
        get_calls = _get_calls(m)
        assert len(get_calls) == 2
        # The retry used the *new* sid.
        assert get_calls[1].kwargs["headers"]["X-FTL-SID"] == "SID2"
    finally:
        await source.close()


async def test_malformed_rows_are_skipped_not_raised():
    source = PiholeSource(url=_URL, password="pw")
    try:
        with aioresponses() as m:
            m.post(_AUTH, payload={"session": {"sid": "SID1"}})
            m.get(
                _QUERIES_RE,
                payload={
                    "queries": [
                        {"time": 1.0, "client": {"ip": "10.0.0.5"}, "domain": "ok.com"},
                        {"client": {"ip": "10.0.0.5"}, "domain": "no-time.com"},  # skip
                        {"time": 2.0, "domain": "no-client.com"},  # skip
                        "not-a-dict",  # skip
                    ]
                },
            )
            await source.login()
            queries = await source.queries_since(0.0)

        assert queries == [DnsQuery(ts=1.0, client_ip="10.0.0.5", domain="ok.com")]
    finally:
        await source.close()
