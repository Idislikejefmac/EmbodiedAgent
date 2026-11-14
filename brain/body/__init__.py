"""Body adapters. The rest of the brain imports `Body` and the data classes from here and gets a body
from `create_body()`; it never imports a particular body (tests/test_boundary_lint.py)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..config import BODY_KINDS, Config, as_config
from .base import (INTERFACE_VERSION, Body, BodyBusy, BodyError, BodyTimeout, BodyUnavailable, NotSupported,
                   NotYetImplemented)
from .streams import BodyStream
from .types import (AudioChunk, BodyEvent, BodyStatus, Capability, ClockSync, ConfidenceBand, Image, NavGoal, Pose,
                    Speaker, TrackingState, Utterance)

__all__ = [
    "BODY_KINDS", "INTERFACE_VERSION", "AudioChunk", "Body", "BodyBusy", "BodyError", "BodyEvent", "BodyStatus",
    "BodyStream", "BodyTimeout", "BodyUnavailable", "Capability", "ClockSync", "ConfidenceBand", "Image",
    "NavGoal", "NotSupported", "NotYetImplemented", "Pose", "Speaker", "TrackingState", "Utterance", "create_body",
]


def create_body(kind: str, config: Config | Mapping[str, Any], *, token: bytes | None = None,
                **options: Any) -> Body:
    """The one place a body is chosen. `kind` is "sim" or "phone" (config.toml `[body] kind`).

    `config` is the checked Config (a raw table goes through the config gate first, so a bad value is a
    ConfigError, a ValueError). The phone body needs the link token; the caller (the wiring point,
    brain/__main__.py) loads it. The phone body's module is imported only when it is asked for.
    """
    cfg = as_config(config)
    if kind == "sim":
        from .sim import SimBody

        options.setdefault("image_dir", cfg.body.sim.image_dir)  # None: built-in synthetic media
        options.setdefault("audio_dir", cfg.body.sim.audio_dir)
        return SimBody(**options)
    if kind == "phone":
        from .phone import PhoneBody

        if token is None:
            raise ValueError("the phone body needs the link token")
        options.setdefault("host", cfg.network.host)
        options.setdefault("port", cfg.network.port)
        return PhoneBody(token=token, **options)
    raise ValueError(f"unknown body kind {kind!r}; expected one of {BODY_KINDS}")
