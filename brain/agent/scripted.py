"""A scripted stand-in for the model (`python -m brain chat --fake-model`; the default without --live).

It needs no key, makes no network call and spends nothing, yet drives the real loop: tool calls go through
the registry, replies are spoken through the body, events, the spend guard (in memory only) and the prompt
capture all run. Its rules are deliberately simple and visible:

- a Poke: a greeting;
- battery or status: calls get_body_status, then says the battery level;
- where: calls get_pose; look or see: calls capture_image; stop or quiet: calls stop;
- delete, rename, file, folder, config, command, run, documents, bin: it behaves like a model that was talked
  into it and calls a tool that does not exist (delete_files, read_file, run_command...), so the registry's
  refusal is visible end to end;
- anything else: says it is a scripted stand-in and repeats how many words it heard.

After a tool result it reports the result in one sentence (a refusal's own message when refused).
"""

from __future__ import annotations

import json
from typing import Any

from .loop import estimate_input_tokens
from .model import ModelReply, ModelRequest, TextSink, ToolCall, Usage

FORBIDDEN: tuple[tuple[str, str, dict[str, Any]], ...] = (("delete", "delete_files", {"what": "everything"}), ("rename", "rename_file", {"name": "a"}),
             ("config", "read_file", {"name": "config"}), ("document", "list_folder", {"name": "documents"}),
             ("bin", "list_quarantine", {}), ("file", "list_files", {}), ("folder", "list_folder", {}),
             ("command", "run_command", {"line": "whoami"}), ("run ", "run_command", {"line": "whoami"}))


def _last_user(request: ModelRequest) -> list[Any]:
    for msg in reversed(request.messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            return list(msg["content"])
    return []


def _say_result(name: str, result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return str(result.get("message") or "I could not do that.")
    content = result.get("result") or {}
    if name == "get_body_status":
        pct = content.get("battery_pct")
        return f"My battery is at {round(pct)} percent." if isinstance(pct, (int, float)) else "I can't read my battery."
    if name == "get_pose":
        return "I know where I am, more or less." if content.get("tracking") == "ok" else "I'm not sure where I am."
    if name == "capture_image":
        return "I took a picture, but I am only a scripted stand-in, so I can't describe it."
    if name == "stop":
        return "I have stopped."
    return "Done."


class ScriptedModel:
    name = "scripted"

    def __init__(self) -> None:
        self.calls = 0
        self._ids = 0
        self._asked: dict[str, str] = {}  # tool call id -> tool name

    def _reply(self, request: ModelRequest, text: str = "", tool: tuple[str, dict[str, Any]] | None = None,
               on_text: TextSink | None = None) -> ModelReply:
        blocks: list[dict[str, Any]] = []
        calls: tuple[ToolCall, ...] = ()
        if text:
            blocks.append({"type": "text", "text": text})
            if on_text is not None:
                for i in range(0, len(text), 12):
                    on_text(text[i:i + 12])
        if tool is not None:
            self._ids += 1
            cid = f"toolu_scripted_{self._ids}"
            self._asked[cid] = tool[0]
            blocks.append({"type": "tool_use", "id": cid, "name": tool[0], "input": tool[1]})
            calls = (ToolCall(cid, tool[0], tool[1]),)
        usage = Usage(input_tok=estimate_input_tokens(request) * 3 // 4, output_tok=len(text) // 4 + (20 if tool else 1))
        return ModelReply(tuple(blocks), text, calls, "tool_use" if tool else "end_turn", usage, events=len(blocks) + 2)

    async def complete(self, request: ModelRequest, on_text: TextSink | None = None) -> ModelReply:
        self.calls += 1
        content = _last_user(request)
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            said = []
            for b in results:
                try:
                    result = json.loads(b["content"][0]["text"])
                except (KeyError, IndexError, TypeError, ValueError):
                    result = {"ok": False}
                said.append(_say_result(self._asked.get(str(b.get("tool_use_id")), "?"), result))
            return self._reply(request, " ".join(said), on_text=on_text)
        text = " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
        words = text.split(":", 1)[-1].strip().lower()
        if text.startswith("[app event]"):
            return self._reply(request, "Hello there! You poked me.", on_text=on_text)
        if "battery" in words or "status" in words:
            return self._reply(request, tool=("get_body_status", {}))
        if "where" in words:
            return self._reply(request, tool=("get_pose", {}))
        if "look" in words or "see" in words:
            return self._reply(request, tool=("capture_image", {"reason": "asked to look"}))
        if "stop" in words or "quiet" in words:
            return self._reply(request, tool=("stop", {}))
        for word, tool, args in FORBIDDEN:
            if word in words:
                return self._reply(request, tool=(tool, args))
        n = len(words.split())
        return self._reply(request, f"I heard {n} word{'s' if n != 1 else ''}. I am a scripted stand-in for the "
                                    f"real model, so that is all I can say.", on_text=on_text)

    async def list_models(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None
