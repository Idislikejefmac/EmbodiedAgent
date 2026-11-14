"""Handlers that connect the agent's body tools to a `Body`, whichever body it is (card P0-06).

Built here: get_body_status, get_pose, capture_image, speak and stop. The others in the catalogue
(listen, who_is_present, navigate_to) need the voice, identity and spatial tracks and get their
handlers there; until then the registry answers them with "not built".

Times given to the agent are ages in milliseconds (`age_ms`), never clock values. Handlers only await
the body; they must never block the event loop (see brain/agent/tools.py).

These are deliberately thin, and two are stand-ins that later cards replace without changing the
tool: get_pose reads one pose from the stream (card P4-01 gives it a pose buffer), and speak uses the
body's own text-to-speech (card P2-05 streams synthesized audio instead).

Nothing in this module touches files, processes or the network: it only calls the Body.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any

from ..body.base import Body
from ..body.types import Capability, Pose
from ..limits import DEFAULT_MAX_EDGE_PX as DEFAULT_MAX_EDGE_PX  # the catalogue's default for capture_image.max_edge
from ..limits import POSE_WAIT_S as POSE_WAIT_S  # re-exported (H-11)
from ..transport.protocol import now_us
from .tools import Caller, ToolOutput, ToolRegistry

log = logging.getLogger("brain.agent.body_tools")


def _age_ms(t_us: int) -> int:
    return max(0, (now_us() - t_us) // 1000)


def _pose_fields(pose: Pose) -> dict[str, Any]:
    x, y, z = pose.position_m
    heading = pose.heading_deg
    return {"tracking": pose.tracking.value, "frame_id": pose.frame_id, "x_m": round(x, 3) + 0.0,
            "y_m": round(y, 3) + 0.0, "z_m": round(z, 3) + 0.0,  # + 0.0 turns -0.0 into 0.0
            "heading_deg": None if heading is None else round(heading, 1) + 0.0}


def register_body_tools(registry: ToolRegistry, body: Body) -> None:
    utterance_numbers = itertools.count(1)

    async def get_body_status(args: dict[str, Any], caller: Caller) -> dict[str, Any]:
        s = await body.status()
        return {"connected": s.connected, "stale": s.stale, "battery_pct": s.battery_pct, "charging": s.charging,
                "temperature_c": s.temperature_c, "link": {"rtt_ms": s.link_rtt_ms},
                "capabilities": list(s.capabilities)}

    async def get_pose(args: dict[str, Any], caller: Caller) -> dict[str, Any]:
        stream = body.pose_stream()
        try:
            pose = await asyncio.wait_for(anext(stream), POSE_WAIT_S)
        finally:
            await stream.aclose()
        return {**_pose_fields(pose), "age_ms": _age_ms(pose.t_us)}

    async def capture_image(args: dict[str, Any], caller: Caller) -> ToolOutput:
        image = await body.capture_image(max_edge_px=args.get("max_edge", DEFAULT_MAX_EDGE_PX))
        log.info("capture_image %s %dx%d (reason given: %d chars)", image.image_id, image.width_px, image.height_px,
                 len(args["reason"]))
        return ToolOutput({"image_id": image.image_id, "encoding": image.encoding, "width_px": image.width_px,
                           "height_px": image.height_px, "age_ms": _age_ms(image.t_us),
                           "camera_pose": None if image.camera_pose is None else _pose_fields(image.camera_pose)},
                          (image,))

    async def speak(args: dict[str, Any], caller: Caller) -> dict[str, Any]:
        body.require(Capability.SPEAK_TEXT)
        utterance_id = f"u-{next(utterance_numbers)}"
        await body.speak_text(args["text"], utterance_id=utterance_id, interrupt=args.get("interrupt", False))
        return {"utterance_id": utterance_id, "started": True}

    async def stop(args: dict[str, Any], caller: Caller) -> dict[str, Any]:
        await body.stop()
        return {"stopped": True}

    for name, handler in (("get_body_status", get_body_status), ("get_pose", get_pose),
                          ("capture_image", capture_image), ("speak", speak), ("stop", stop)):
        registry.register(name, handler)
