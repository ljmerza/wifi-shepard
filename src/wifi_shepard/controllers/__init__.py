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
        return UniFiController(
            name=spec.name,
            host=spec.host,
            username=spec.username,
            password=spec.password,
            site=spec.site,
            verify_ssl=spec.verify_ssl,
        )
    raise ValueError(f"unknown controller type: {spec.type!r}")
