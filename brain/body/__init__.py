"""Body adapters. The rest of the brain imports `Body` and the data classes from here and gets a body
from `create_body()`; it never imports a particular body (tests/test_boundary_lint.py)."""

from __future__ import annotations

from typing import Any

from ..config import REPO_ROOT
from .base import (INTERFACE_VERSION, Body, BodyBusy, BodyError, BodyTimeout, BodyUnavailable, NotSupported,
                   NotYetImplemented)
from .streams import BodyStream
from .types import (AudioChunk, BodyEvent, BodyStatus, Capability, ClockSync, ConfidenceBand, Image, NavGoal, Pose,
                    Speaker, TrackingState, Utterance)

BODY_KINDS = ("sim", "phone")

__all__ = [
    "BODY_KINDS", "INTERFACE_VERSION", "AudioChunk", "Body", "BodyBusy", "BodyError", "BodyEvent", "BodyStatus",
    "BodyStream", "BodyTimeout", "BodyUnavailable", "Capability", "ClockSync", "ConfidenceBand", "Image",
    "NavGoal", "NotSupported", "NotYetImplemented", "Pose", "Speaker", "TrackingState", "Utterance", "create_body",
]


def create_body(kind: str, config: dict[str, Any], *, token: bytes | None = None, **options: Any) -> Body:
    """The one place a body is chosen. `kind` is "sim" or "phone" (config.toml `[body] kind`).

    The phone body needs the link token; the caller (the wiring point, brain/__main__.py) loads it.
    The phone body's module is imported only when it is asked for, so the brain runs without it.
    """
    if kind == "sim":
        from .sim import SimBody

        sim = config.get("body", {}).get("sim", {})
        folders = {k: REPO_ROOT / sim[k] for k in ("image_dir", "audio_dir") if sim.get(k)}
        return SimBody(**folders, **options)
    if kind == "phone":
        from .phone import PhoneBody

        if token is None:
            raise ValueError("the phone body needs the link token")
        net = config.get("network", {})
        options.setdefault("host", net.get("host", "127.0.0.1"))
        options.setdefault("port", int(net.get("port", 8765)))
        return PhoneBody(token=token, **options)
    raise ValueError(f"unknown body kind {kind!r}; expected one of {BODY_KINDS}")
