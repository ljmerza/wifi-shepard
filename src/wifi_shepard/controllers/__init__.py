from __future__ import annotations

from typing import TYPE_CHECKING

from .base import APSnapshot, ClientSnapshot, Controller, RadioStats
from .unifi import UniFiController, UniFiSchemaError

if TYPE_CHECKING:
    from ..config import ControllerSpec

__all__ = [
    "APSnapshot",
    "ClientSnapshot",
    "Controller",
    "RadioStats",
    "UniFiController",
    "UniFiSchemaError",
    "build_controller",
]


def build_controller(spec: ControllerSpec) -> Controller:
    if spec.type == "unifi":
        # port=None means "backend default" — omit the kwarg so UniFiController's
        # own default (8443) stays the single source of truth.
        port_kwarg: dict[str, int] = {} if spec.port is None else {"port": spec.port}
        return UniFiController(
            name=spec.name,
            host=spec.host,
            username=spec.username,
            password=spec.password,
            site=spec.site,
            verify_ssl=spec.verify_ssl,
            **port_kwarg,
        )
    raise ValueError(f"unknown controller type: {spec.type!r}")
