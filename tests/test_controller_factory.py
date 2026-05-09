from __future__ import annotations

import pytest


def _spec(**overrides):
    from wifi_shepard.config import ControllerSpec

    base = dict(
        type="unifi",
        name="home",
        host="192.168.1.1",
        username="shepard",
        password="secret",
        site="default",
        verify_ssl=False,
    )
    base.update(overrides)
    return ControllerSpec(**base)


def test_factory_builds_unifi_controller_with_forwarded_kwargs():
    from wifi_shepard.controllers import UniFiController, build_controller

    spec = _spec()
    controller = build_controller(spec)

    assert isinstance(controller, UniFiController)
    assert controller.host == "192.168.1.1"
    assert controller.username == "shepard"
    assert controller.password == "secret"
    assert controller.site == "default"
    assert controller.verify_ssl is False
    assert controller.name == "home"


def test_factory_rejects_unknown_type_with_value_error():
    from wifi_shepard.controllers import build_controller

    spec = _spec(type="omada")
    with pytest.raises(ValueError, match="unknown controller type"):
        build_controller(spec)
