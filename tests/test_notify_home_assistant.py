"""HomeAssistantNotifier — HA REST notify transport (PLAN.md §1/§4).

HTTP behavior is exercised through aioresponses; delivery must be best-effort
(a down HA never raises into the kick path).
"""

from __future__ import annotations

import aiohttp
from aioresponses import aioresponses
from yarl import URL

from wifi_shepard.config import HomeAssistantConfig
from wifi_shepard.notify import HomeAssistantNotifier, Notifier

_ENDPOINT = "http://ha.local:8123/api/services/notify/mobile_app_pixel"
_MAC = "aa:bb:cc:dd:ee:ff"


def _notifier(url: str = "http://ha.local:8123") -> HomeAssistantNotifier:
    return HomeAssistantNotifier(
        HomeAssistantConfig(url=url, token="secret-token", notify_service="mobile_app_pixel")
    )


def test_implements_notifier_protocol():
    assert isinstance(_notifier(), Notifier)


async def test_kick_posts_to_notify_service_with_bearer_token():
    notifier = _notifier()
    try:
        with aioresponses() as mocked:
            mocked.post(_ENDPOINT, status=200)
            await notifier.notify(_MAC, severity="kick")
            calls = mocked.requests[("POST", URL(_ENDPOINT))]
            assert len(calls) == 1
            kwargs = calls[0].kwargs
            assert kwargs["headers"]["Authorization"] == "Bearer secret-token"
            assert _MAC in kwargs["json"]["message"]
            assert "kick" in kwargs["json"]["title"]
    finally:
        await notifier.close()


async def test_quarantine_message_flags_possibly_defective_device():
    notifier = _notifier()
    try:
        with aioresponses() as mocked:
            mocked.post(_ENDPOINT, status=200)
            await notifier.notify(_MAC, severity="quarantine")
            kwargs = mocked.requests[("POST", URL(_ENDPOINT))][0].kwargs
            assert _MAC in kwargs["json"]["message"]
            assert "defective" in kwargs["json"]["message"]
    finally:
        await notifier.close()


async def test_unknown_severity_still_delivers():
    notifier = _notifier()
    try:
        with aioresponses() as mocked:
            mocked.post(_ENDPOINT, status=200)
            await notifier.notify(_MAC, severity="reboot")
            kwargs = mocked.requests[("POST", URL(_ENDPOINT))][0].kwargs
            assert _MAC in kwargs["json"]["message"]
    finally:
        await notifier.close()


async def test_http_error_status_is_swallowed_and_logged(caplog):
    notifier = _notifier()
    try:
        with aioresponses() as mocked:
            mocked.post(_ENDPOINT, status=500)
            await notifier.notify(_MAC, severity="kick")  # must not raise
        assert any(r.message == "notify_failed" for r in caplog.records)
    finally:
        await notifier.close()


async def test_connection_error_is_swallowed_and_logged(caplog):
    notifier = _notifier()
    try:
        with aioresponses() as mocked:
            mocked.post(_ENDPOINT, exception=aiohttp.ClientConnectionError("boom"))
            await notifier.notify(_MAC, severity="kick")  # must not raise
        assert any(r.message == "notify_failed" for r in caplog.records)
    finally:
        await notifier.close()


async def test_trailing_slash_in_url_is_normalized():
    notifier = _notifier(url="http://ha.local:8123/")
    try:
        with aioresponses() as mocked:
            mocked.post(_ENDPOINT, status=200)
            await notifier.notify(_MAC, severity="kick")
            assert ("POST", URL(_ENDPOINT)) in mocked.requests
    finally:
        await notifier.close()


async def test_close_before_any_notify_is_safe():
    notifier = _notifier()
    await notifier.close()
    await notifier.close()  # idempotent
