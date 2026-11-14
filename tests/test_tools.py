"""The agent's tool schemas and the default-deny registry (card P0-06 steps 5 and 8; plan: "Command boundary")."""

import ast
import asyncio
import copy
import json
import logging
import re
import sys
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from brain.agent import body_tools
from brain.agent.body_tools import register_body_tools
from brain.agent.tools import (APP, CATALOGUE_PATH, CONSOLE, MAX_ARGS_BYTES, MESSAGES, Caller, Catalogue, Channel,
                               InputSource, ToolOutput, ToolRegistry, ToolResult, catalogue_problems, load_catalogue,
                               turn_channel)
from brain.body import (BodyBusy, BodyError, BodyUnavailable, Capability, ConfidenceBand, Image, NotSupported,
                        NotYetImplemented)
from brain.body.phone import PhoneBody
from brain.body.sim import SimBody
from brain.transport.client import TestBodyClient

EIGHT = {"get_pose", "capture_image", "speak", "listen", "who_is_present", "navigate_to", "stop", "get_body_status"}
RESERVED = {"where_am_i", "route_to", "describe_place", "list_waypoints", "recall", "save_waypoint", "remember",
            "set_quiet", "start_enrollment", "delete_person_data"}
RAW = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
TOKEN = bytes(range(32))


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


# ---- the schemas -----------------------------------------------------------------------------------------

def test_catalogue_lists_exactly_the_eight_tools_and_the_reserved_names():
    cat = load_catalogue()
    assert set(cat.tools) == EIGHT and set(cat.reserved) == RESERVED
    assert catalogue_problems(RAW) == []
    # Every name the plan's Command boundary table allows is either listed or reserved; nothing else is.
    boundary_table = EIGHT | RESERVED
    assert set(cat.tools) | set(cat.reserved) == boundary_table


@pytest.mark.parametrize("entry", RAW["tools"], ids=lambda e: e["name"])
def test_each_tool_schema_is_valid_json_schema_closed_and_flagged(entry):
    for key in ("input_schema", "output_schema"):
        Draft202012Validator.check_schema(entry[key])
        assert entry[key]["type"] == "object" and entry[key]["additionalProperties"] is False
    assert isinstance(entry["sensitive"], bool)
    assert set(entry["channels"]) <= {"app", "console"} and "console" in entry["channels"]
    assert entry["class"] in ("query", "teaching", "motion", "staged_delete")
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", entry["name"])
    assert len(entry["description"]) >= 40


def test_sensitive_and_channel_flags_follow_the_plan():
    cat = load_catalogue()
    assert not any(cat.tools[n].sensitive for n in EIGHT)  # conversation, read-only queries, stop
    assert {n for n, s in cat.reserved.items() if s.sensitive} == {"start_enrollment", "delete_person_data"}
    assert all(Channel.APP in s.channels and Channel.CONSOLE in s.channels for s in cat.tools.values())
    assert all(s.uses_body for s in cat.tools.values())


VALID = {
    "get_pose": [{}], "who_is_present": [{}], "stop": [{}], "get_body_status": [{}],
    "capture_image": [{"reason": "to see who is here"}, {"reason": "read a label", "max_edge": 1568}],
    "speak": [{"text": "Hello."}, {"text": "Wait.", "interrupt": True}, {"text": "One.\nTwo, caf\u00e9."}],
    "listen": [{}, {"timeout_s": 5}, {"timeout_s": 2.5}],
    "navigate_to": [{"waypoint": "kitchen"}, {"waypoint": "Mom's room"}, {"waypoint": "2nd bedroom"},
                    {"waypoint": "caf\u00e9"}],
}
INVALID = {
    "get_pose": [{"frame": "x"}], "who_is_present": [{"path": "C:\\"}], "stop": [{"force": True}],
    "get_body_status": [{"verbose": True}, {"command": "systeminfo"}],
    "capture_image": [{}, {"reason": ""}, {"reason": "x", "max_edge": 32}, {"reason": "x", "max_edge": 640.0},
                      {"reason": "x", "save_to": "C:\\out.jpg"}, {"reason": "r" * 201},
                      {"reason": "x", "max_edge": 4096},  # above 1568 costs more and shows the model no more
                      {"reason": "x", "max_edge": True}, {"reason": "x", "max_edge": 10**400},
                      {"reason": "x", "max_edge": float("nan")}, {"reason": "bell\x07"}],
    "speak": [{}, {"text": ""}, {"text": 5}, {"text": "hi", "interrupt": "yes"}, {"text": "x" * 2001},
              {"text": "hi", "voice_file": "..\\v.wav"}, {"text": "hi", "interrupt": 1}, {"text": ["hi"]},
              {"text": "nul\x00"}, {"text": "x" * 10_000_000}, {"text": "hi", "caller": "console"},
              {1: "x", "text": "hi"}, {("tup",): "x", "text": "hi"}],
    "listen": [{"timeout_s": 0}, {"timeout_s": 600}, {"timeout_s": "10"}],
    "navigate_to": [{}, {"waypoint": ""}, {"waypoint": "..\\..\\config.toml"}, {"waypoint": "C:\\Users"},
                    {"waypoint": "/etc/passwd"}, {"waypoint": "a/b"}, {"waypoint": "kitchen\n"}, {"waypoint": "x" * 65},
                    {"waypoint": ".."}, {"waypoint": "kitchen", "speed": 9}, {"waypoint": "%APPDATA%"},
                    {"waypoint": "a%APPDATA%"}, {"waypoint": "a\u202e"},  # right-to-left override
                    {"waypoint": "kit\u200bchen"},  # zero-width space
                    {"waypoint": "\uff4b\uff49\uff54\uff43\uff48\uff45\uff4e"},  # full-width letters: not NFKC
                    {"waypoint": "a\x7f"}],
}


@pytest.mark.parametrize("name", sorted(EIGHT))
def test_tool_arguments_validate_by_schema(name):
    registry = ToolRegistry()

    async def echo(args, caller):
        raise NotSupported(Capability.POSE)  # reaching the handler is what this test detects

    registry.register(name, echo)

    async def go():
        for args in VALID[name]:
            assert (await registry.call(name, args, APP)).code == "body_not_supported", args
        for args in INVALID[name]:
            r = await registry.call(name, args, APP)
            assert (r.status, r.code) == ("refused", "invalid_arguments"), args

    run(go())


def bad_catalogue(edit):
    raw = copy.deepcopy(RAW)
    edit(raw)
    return catalogue_problems(raw)


def test_catalogue_rules_catch_a_tool_that_breaks_the_boundary():
    def path_param(raw):
        raw["tools"][1]["input_schema"]["properties"]["save_path"] = {"type": "string", "x-kind": "name",
                                                                      "maxLength": 9, "pattern": "^a$"}

    def untyped_string(raw):
        raw["tools"][2]["input_schema"]["properties"]["text"] = {"type": "string"}

    def open_schema(raw):
        del raw["tools"][0]["input_schema"]["additionalProperties"]

    def name_without_pattern(raw):
        del raw["tools"][5]["input_schema"]["properties"]["waypoint"]["pattern"]

    def app_only(raw):
        raw["tools"][6]["channels"] = ["app"]

    def any_of(raw):
        raw["tools"][2]["input_schema"]["properties"]["text"] = {"anyOf": [{"type": "string"}, {"type": "integer"}]}

    def ref(raw):
        raw["tools"][2]["input_schema"]["properties"]["text"] = {"$ref": "#/$defs/t"}
        raw["tools"][2]["input_schema"]["$defs"] = {"t": {"type": "string"}}

    def pattern_properties(raw):
        raw["tools"][0]["input_schema"]["patternProperties"] = {"^x": {"type": "string"}}

    def prefix_items(raw):
        raw["tools"][0]["input_schema"]["properties"]["tags"] = {"type": "array", "prefixItems": [{"type": "string"}]}

    def schema_valued_additional(raw):
        raw["tools"][0]["input_schema"]["additionalProperties"] = {"type": "string"}

    def untyped(raw):
        raw["tools"][0]["input_schema"]["properties"]["anything"] = {"description": "no type at all"}

    def string_in_array(raw):
        raw["tools"][0]["input_schema"]["properties"]["tags"] = {"type": "array", "items": {"type": "string"}}

    def bad_timeout(raw):
        raw["tools"][3]["timeout_plus_arg"] = "no_such_argument"

    def duplicate(raw):
        raw["reserved"].append({"name": "stop", "class": "motion", "channels": ["console"], "sensitive": False})

    def bad_name(raw):
        raw["reserved"].append({"name": "Run Shell", "class": "query", "channels": ["console"], "sensitive": False})

    def no_flag(raw):
        del raw["tools"][7]["sensitive"]

    for edit, expect in ((path_param, "looks like a path"), (untyped_string, "x-kind"), (untyped_string, "maxLength"),
                         (open_schema, "additionalProperties"), (name_without_pattern, "pattern"),
                         (app_only, "console"), (duplicate, "listed twice"), (bad_name, "bad tool name"),
                         (no_flag, "sensitive"), (any_of, "anyOf"), (ref, "$ref"),
                         (pattern_properties, "patternProperties"), (prefix_items, "prefixItems"),
                         (schema_valued_additional, "additionalProperties"), (untyped, "must state its type"),
                         (string_in_array, "x-kind"), (bad_timeout, "timeout_plus_arg")):
        problems = bad_catalogue(edit)
        assert any(expect in p for p in problems), (edit.__name__, problems)
    with pytest.raises(ValueError, match="looks like a path"):
        raw = copy.deepcopy(RAW)
        path_param(raw)
        Catalogue.from_json(raw)


def test_model_tools_are_plain_json_without_private_annotations():
    registry = ToolRegistry()
    assert registry.model_tools(Channel.APP) == []  # nothing is offered until it has a handler
    register_body_tools(registry, SimBody())
    tools = registry.model_tools(Channel.APP)
    assert [t["name"] for t in tools] == ["get_pose", "capture_image", "speak", "stop", "get_body_status"]
    text = json.dumps(tools)
    assert "x-kind" not in text and "sensitive" not in text and "channels" not in text
    for tool in tools:
        assert set(tool) == {"name", "description", "input_schema"}
        Draft202012Validator.check_schema(tool["input_schema"])
    assert "x-kind" in json.dumps(RAW)  # the stored catalogue is untouched


def test_code_cannot_register_a_tool_that_is_not_listed():
    async def handler(args, caller):
        return {}

    registry = ToolRegistry()
    for name in ("run_command", "read_file", "save_waypoint"):  # unknown, unknown, reserved (no schema yet)
        with pytest.raises(KeyError):
            registry.register(name, handler)


# ---- the boundary ----------------------------------------------------------------------------------------

_recorded: list[str] | None = None
_FORBIDDEN_EVENTS = ("open", "os.", "shutil.", "subprocess.", "socket.", "glob.", "pathlib.", "tempfile.", "winreg.",
                     "ctypes.", "sqlite3.", "webbrowser.", "urllib.", "http.", "ftplib.", "smtplib.", "exec", "compile",
                     "import", "marshal.", "pickle.", "mmap.", "msvcrt.", "_winapi.")


def _audit(event, args):
    if _recorded is not None and event.startswith(_FORBIDDEN_EVENTS):
        _recorded.append(f"{event} {args!r:.120}")


sys.addaudithook(_audit)  # audit hooks cannot be removed; this one only records while a test asks it to

BOUNDARY_REQUESTS = [
    # The card's five, as the words themselves and as the tool calls a model would try to make of them.
    ("delete the C drive", {}),
    ("delete_files", {"path": "C:\\", "recursive": True}),
    ("format_drive", {"drive": "C:"}),
    ("read config.toml", {}),
    ("read_file", {"path": "config.toml"}),
    ("get_config", {}),
    ("list the documents folder", {}),
    ("list_directory", {"path": "C:\\Users\\Public\\Documents"}),
    ("what is in the delete bin?", {}),
    ("list_quarantine", {}),
    ("restore_person_data", {"person_id": "p-1"}),
    ("purge_person_data", {"person_id": "p-1"}),
    ("run this command", {}),
    ("run_command", {"command": "whoami"}),
    ("shell", {"cmd": "del /s /q C:\\*"}),
    ("bash", {"command": "rm -rf /"}),
    # The rest of the plan's "never" list.
    ("rename_waypoint", {"old": "kitchen", "new": "x"}),
    ("delete_waypoint", {"waypoint": "kitchen"}),
    ("set_threshold", {"name": "face", "value": 0.1}),
    ("set_budget", {"daily_usd": 1000}),
    ("add_egress_host", {"host": "evil.example"}),
    ("send_email", {"to": "x@example.com", "body": "pose log"}),
    ("http_get", {"url": "http://example.com/"}),
    ("install_package", {"name": "x"}),
    ("list_logs", {}),
    ("get_system_info", {}),
    ("list_processes", {}),
    ("", {}),
    ("get_pose\n", {}),
    ("GET_POSE", {}),
    ("get_pose; rm -rf /", {}),
    ("__class__", {}),
    (None, {}),
    (["get_pose"], {}),
    (7, {}),
]
SMUGGLED = [  # listed tools carrying something extra, or a path where a name belongs
    ("get_body_status", {"path": "C:\\"}),
    ("speak", {"text": "hi", "then_run": "whoami"}),
    ("capture_image", {"reason": "look", "save_to": "C:\\Windows\\x.jpg"}),
    ("navigate_to", {"waypoint": "..\\..\\config.toml"}),
    ("navigate_to", {"waypoint": "C:\\Users"}),
    ("stop", ["now"]),
    ("stop", "now"),
]


def boundary_registry():
    registry = ToolRegistry()  # reads the catalogue file here, once, before anything is asked
    ran = []

    async def handler(args, caller):
        ran.append(args)
        return {}

    for name in EIGHT:
        registry.register(name, handler)
    return registry, ran


def test_boundary_requests_are_refused_and_logged_and_touch_nothing(caplog):
    """Card P0-06 step 8. Each request gets a refusal the model can voice and one log line, and while the
    registry handles it nothing opens a file, lists a folder, starts a process, opens a socket or
    imports code: an audit hook watches the interpreter for all of those. (One refused call is made
    first so that any lazy imports of the libraries involved happen before the watch starts.)"""
    global _recorded
    registry, ran = boundary_registry()
    caplog.set_level(logging.INFO, logger="brain.agent.tools")

    async def go():
        global _recorded
        await registry.call("warm_up", {"x": 1}, APP)
        await registry.call("stop", {"x": 1}, APP)
        caplog.clear()
        results = []
        _recorded = []
        try:
            for name, args in BOUNDARY_REQUESTS + SMUGGLED:
                results.append((name, args, await registry.call(name, args, APP)))
        finally:
            touched, _recorded = _recorded, None
        return results, touched

    results, touched = run(go())
    assert touched == []
    assert ran == []  # no handler ran
    assert len(results) == len(BOUNDARY_REQUESTS) + len(SMUGGLED) and registry.refusals == len(results) + 2
    refusal_lines = [r for r in caplog.records if r.levelno == logging.WARNING and r.getMessage().startswith("REFUSED ")]
    assert len(refusal_lines) == len(results)  # one log line each
    for (name, args, result), record in zip(results, refusal_lines):
        assert isinstance(result, ToolResult) and not result.ok and result.status == "refused", name
        assert result.code == ("unknown_tool" if (name, args) in BOUNDARY_REQUESTS else "invalid_arguments"), name
        assert result.content is None and result.attachments == ()
        assert len(result.message) > 20 and result.message[0] == "I"  # something the agent can say
        assert result.detail is None or len(result.detail) < 200
        said = result.to_json()
        assert said["ok"] is False and said["status"] == "refused" and said["message"] == result.message
        json.dumps(said)
        line = record.getMessage()
        assert "channel=app" in line and f"code={result.code}" in line
    all_logs = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("whoami", "C:\\\\Users", "rm -rf /\"", "x@example.com", "evil.example", "del /s"):
        assert secret not in all_logs  # argument values are never logged, only their keys


def test_the_five_requests_from_the_card_by_name(caplog):
    registry, ran = boundary_registry()
    caplog.set_level(logging.WARNING, logger="brain.agent.tools")

    async def go():
        for words in ("delete the C drive", "read config.toml", "list the documents folder",
                      "what is in the delete bin?", "run this command"):
            caplog.clear()
            result = await registry.call(words, {}, APP)
            assert (result.status, result.code) == ("refused", "unknown_tool")
            assert "cannot act on, or look into, the computer I run on" in result.message
            assert [r.getMessage() for r in caplog.records] == [
                f"REFUSED tool={words!r} channel=app band=unknown code=unknown_tool arg_keys=[]"]
        # Identity does not widen the allowlist, and neither does the console: unlisted is unlisted.
        jeff = Caller(Channel.APP, ConfidenceBand.CONFIRMED, {"voice", "face"})
        for caller in (jeff, CONSOLE):
            assert (await registry.call("run_command", {"command": "dir"}, caller)).code == "unknown_tool"

    run(go())
    assert ran == []


def test_a_log_line_cannot_be_forged_through_a_tool_name(caplog):
    registry, _ = boundary_registry()
    caplog.set_level(logging.WARNING, logger="brain.agent.tools")
    run(registry.call("x\nREFUSED tool='stop' channel=console", {"a\nb": 1}, APP))
    (line,) = [r.getMessage() for r in caplog.records]
    assert "\n" not in line and line.startswith('REFUSED tool="x\\nREFUSED') and line.count("code=") == 1


def test_reserved_tools_exist_by_name_but_are_not_built():
    registry, ran = boundary_registry()
    sensitive = {"start_enrollment", "delete_person_data"}

    async def go():
        for name in RESERVED - sensitive:
            r = await registry.call(name, {"person_id": "p-1"}, APP)
            assert (r.status, r.code) == ("unavailable", "not_built"), name
        r = await ToolRegistry().call("get_pose", {}, APP)  # listed, but no handler registered
        assert (r.status, r.code) == ("unavailable", "not_built")
        assert registry.refusals == 0
        # The boundary is checked before "not built": an unidentified request for a sensitive tool is a
        # refusal, logged and counted, even though the tool does not exist yet.
        jeff = Caller(Channel.APP, ConfidenceBand.CONFIRMED, {"voice", "face"}, "p-jeff")
        for name in sensitive:
            for caller in (APP, Caller(Channel.APP, ConfidenceBand.CONFIRMED, {"voice"}),
                           Caller(Channel.APP, ConfidenceBand.PROBABLE, {"voice", "face"})):
                r = await registry.call(name, {"person_id": "p-1"}, caller)
                assert (r.status, r.code) == ("refused", "identity_not_confirmed"), name
            for caller in (jeff, CONSOLE):
                assert (await registry.call(name, {"person_id": "p-1"}, caller)).code == "not_built"
        assert registry.refusals == 6

    run(go())
    assert ran == []


def test_set_quiet_is_reserved_for_the_app_channel_and_is_not_sensitive():
    spec = load_catalogue().reserved["set_quiet"]  # Jeff: its own persistent tool, built at P5-02; stop stays one-shot
    assert spec.channels == {Channel.APP, Channel.CONSOLE} and not spec.sensitive and spec.card == "P5-02"


def custom_registry(**flags):
    raw = {"version": 0, "reserved": [], "tools": [{
        "name": "test_tool", "class": "teaching", "channels": ["app", "console"], "sensitive": False,
        "description": "A tool that exists only in this test of the registry.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"person_id": {"type": "string", "x-kind": "id", "maxLength": 64,
                                                      "pattern": "^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}(?!\\n)$"}}},
        "output_schema": {"type": "object", "additionalProperties": False, "required": ["done"],
                          "properties": {"done": {"type": "boolean"}}}} | flags]}
    return ToolRegistry(Catalogue.from_json(raw), timeout_s=0.2)


def test_sensitive_tools_need_a_confirmed_identity_on_both_face_and_voice():
    registry = custom_registry(sensitive=True)

    async def handler(args, caller):
        return {"done": True}

    registry.register("test_tool", handler)
    both = {"voice", "face"}

    async def go():
        refused = [APP, Caller(Channel.APP, ConfidenceBand.CONFIRMED, {"voice"}),  # voice alone is never enough
                   Caller(Channel.APP, ConfidenceBand.CONFIRMED, {"face"}),
                   Caller(Channel.APP, ConfidenceBand.PROBABLE, both), Caller(Channel.APP, ConfidenceBand.UNCERTAIN, both)]
        for caller in refused:
            r = await registry.call("test_tool", {}, caller)
            assert (r.status, r.code) == ("refused", "identity_not_confirmed"), caller
        for caller in (Caller(Channel.APP, ConfidenceBand.CONFIRMED, both), CONSOLE):
            assert (await registry.call("test_tool", {}, caller)).ok, caller

    run(go())
    assert registry.refusals == 5


def test_console_only_tools_are_refused_on_the_app_channel_whoever_asks():
    registry = custom_registry(channels=["console"])

    async def handler(args, caller):
        return {"done": True}

    registry.register("test_tool", handler)

    async def go():
        jeff = Caller(Channel.APP, ConfidenceBand.CONFIRMED, {"voice", "face"})
        r = await registry.call("test_tool", {}, jeff)
        assert (r.status, r.code) == ("refused", "channel_not_permitted") and "console" in r.message
        assert (await registry.call("test_tool", {}, CONSOLE)).ok
        assert registry.available(Channel.APP) == [] and registry.available(Channel.CONSOLE) == ["test_tool"]
        assert registry.model_tools(Channel.APP) == []

    run(go())


def test_id_parameters_refuse_paths():
    registry = custom_registry()
    seen = []

    async def handler(args, caller):
        seen.append(args)
        return {"done": True}

    registry.register("test_tool", handler)

    async def go():
        assert (await registry.call("test_tool", {"person_id": "p-jeff"}, APP)).ok
        for bad in ("..\\registry", "C:\\Users\\x", "/etc/passwd", "p jeff", "", ".hidden", "p-1\n"):
            assert (await registry.call("test_tool", {"person_id": bad}, APP)).code == "invalid_arguments", bad

    run(go())
    assert seen == [{"person_id": "p-jeff"}]


def test_handler_failures_become_structured_results_never_exceptions(caplog):
    registry = custom_registry()

    def failing(exc):
        async def handler(args, caller):
            raise exc
        return handler

    async def slow(args, caller):
        await asyncio.sleep(5)

    async def wrong_shape(args, caller):
        return {"done": "yes", "extra": 1}

    async def bad_attachment(args, caller):
        return ToolOutput({"done": True}, attachments=["not an image"])

    async def not_a_dict(args, caller):
        return ["done"]

    def not_a_coroutine(args, caller):
        return {"done": True}

    async def go():
        cases = [(failing(NotSupported(Capability.IMAGE)), "unavailable", "body_not_supported"),
                 (failing(NotYetImplemented(Capability.IMAGE, "card P1-01")), "unavailable", "not_built"),
                 (failing(BodyUnavailable("gone")), "unavailable", "body_unavailable"),
                 (failing(BodyBusy("busy")), "unavailable", "busy"),
                 (failing(BodyError("odd", code="bad_frame")), "error", "body_error"),
                 (failing(RuntimeError("secret detail")), "error", "internal"),
                 (slow, "unavailable", "timeout"), (wrong_shape, "error", "internal"),
                 (bad_attachment, "error", "internal"), (not_a_dict, "error", "internal"),
                 (not_a_coroutine, "error", "internal")]
        for handler, status, code in cases:
            registry.register("test_tool", handler)
            r = await registry.call("test_tool", {}, APP)
            assert (r.status, r.code) == (status, code) and r.content is None
            assert "secret detail" not in json.dumps(r.to_json())

    caplog.set_level(logging.CRITICAL)
    run(go())
    assert registry.refusals == 0  # these are failures, not refusals


def test_tool_result_json_shapes():
    ok = ToolResult("stop", "ok", "ok", "", {"stopped": True})
    assert ok.ok and ok.to_json() == {"ok": True, "status": "ok", "code": "ok", "result": {"stopped": True}}
    no = ToolResult("x", "refused", "invalid_arguments", "I could not.", detail="unexpected argument(s) ['a']")
    assert no.to_json() == {"ok": False, "status": "refused", "code": "invalid_arguments", "message": "I could not.",
                            "detail": "unexpected argument(s) ['a']"}


# ---- the body tools against real bodies --------------------------------------------------------------------

def test_body_tools_run_end_to_end_against_the_sim_body():
    async def go():
        registry = ToolRegistry()
        async with SimBody() as sim:
            register_body_tools(registry, sim)
            status = await registry.call("get_body_status", {}, APP)
            assert status.ok and status.content["battery_pct"] == 87.0 and status.content["connected"] is True
            assert status.content["stale"] is False
            assert "pose" in status.content["capabilities"]
            pose = await registry.call("get_pose", {}, APP)
            assert pose.ok and pose.content["frame_id"] == sim.frame_id and pose.content["tracking"] == "ok"
            assert pose.content["y_m"] == 1.4 and 0 <= pose.content["age_ms"] < 2000
            look = await registry.call("capture_image", {"reason": "test", "max_edge": 64}, APP)
            assert look.ok and look.content["width_px"] == 64
            assert look.content["camera_pose"]["frame_id"] == sim.frame_id
            (image,) = look.attachments
            assert isinstance(image, Image) and image.image_id == look.content["image_id"] and image.data
            assert "data" not in json.dumps(look.to_json())  # the picture travels beside the JSON, not in it
            said = await registry.call("speak", {"text": "Hello Jeff."}, APP)
            assert said.ok and sim.spoken == [(said.content["utterance_id"], "Hello Jeff.", False)]
            stopped = await registry.call("stop", {}, APP)
            assert stopped.content == {"stopped": True} and sim.stops == [(True, True)]
            for name, args in (("listen", {}), ("who_is_present", {}), ("navigate_to", {"waypoint": "kitchen"})):
                assert (await registry.call(name, args, APP)).code == "not_built"

    run(go())


def test_body_tools_report_what_a_lesser_body_cannot_do():
    async def go():
        registry = ToolRegistry()
        async with SimBody(capabilities=()) as bare:
            register_body_tools(registry, bare)
            assert (await registry.call("get_body_status", {}, APP)).content["capabilities"] == []
            assert (await registry.call("stop", {}, APP)).ok
            for name, args in (("get_pose", {}), ("capture_image", {"reason": "x"}), ("speak", {"text": "hi"})):
                r = await registry.call(name, args, APP)
                assert (r.status, r.code) == ("unavailable", "body_not_supported"), name
                assert r.message == "My body cannot do that."
        r = await registry.call("stop", {}, APP)  # the body is closed now
        assert (r.status, r.code) == ("unavailable", "body_unavailable")
        assert (await registry.call("get_body_status", {}, APP)).content["connected"] is False

    run(go())


def test_body_tools_against_the_phone_body_over_loopback():
    """The same registry and handlers, a different body: nothing above the Body can tell."""
    async def go():
        registry = ToolRegistry()
        body = PhoneBody(token=TOKEN, port=0, phone_link=False, heartbeat_s=0.1, liveness_s=3.0)
        register_body_tools(registry, body)
        async with body:
            offline = await registry.call("get_body_status", {}, APP)
            assert offline.ok and offline.content["connected"] is False and offline.content["battery_pct"] is None
            async with TestBodyClient(f"ws://127.0.0.1:{body.port}/", token=TOKEN, heartbeat_s=0.1, liveness_s=3.0):
                assert await body.wait_connected(5)
                status = await registry.call("get_body_status", {}, APP)
                assert status.ok and status.content == {"connected": True, "stale": False, "battery_pct": 100.0,
                                                        "charging": True,
                                                        "temperature_c": None, "link": status.content["link"],
                                                        "capabilities": []}
                pose = await registry.call("get_pose", {}, APP)
                # Not built for this body yet: "not_built", and the model learns only which capability.
                assert (pose.status, pose.code, pose.detail) == ("unavailable", "not_built", "pose")
                assert (await registry.call("speak", {"text": "hi"}, APP)).code == "body_not_supported"
                assert (await registry.call("stop", {}, APP)).ok

    run(go())


def test_body_tool_handlers_import_nothing_that_could_touch_the_system():
    """The handlers' module may use only asyncio, a counter, logging, the brain's own body layer and the named
    limits (brain/limits.py holds only constants, H-11)."""
    tree = ast.parse(Path(body_tools.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    assert imported == {"__future__", "asyncio", "itertools", "logging", "typing", "..body.base", "..body.types",
                        "..transport.protocol", ".tools", "..limits"}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree)
                                                                         if isinstance(n, ast.Attribute)}
    assert not names & {"open", "exec", "eval", "compile", "__import__", "system", "Popen", "getattr"}


def test_tool_output_wrapper_and_default_edge_match_the_schema():
    assert ToolOutput({"a": 1}).attachments == ()
    edge = load_catalogue().tools["capture_image"].input_schema["properties"]["max_edge"]["default"]
    assert edge == body_tools.DEFAULT_MAX_EDGE_PX


# ---- regressions from the second-opinion review (13 Nov 2026) ------------------------------------------------

def test_the_registry_never_raises_whatever_the_arguments(caplog):
    """Arguments that used to escape as exceptions (RecursionError from deep nesting, a dict subclass
    whose iteration raises) are refused like any other bad request, quickly, and without huge output."""
    registry, ran = boundary_registry()
    caplog.set_level(logging.WARNING, logger="brain.agent.tools")

    deep_dict: dict = {}
    cursor = deep_dict
    for _ in range(50_000):
        cursor["a"] = {}
        cursor = cursor["a"]
    deep_list: list = []
    for _ in range(50_000):
        deep_list = [deep_list]

    class Evil(dict):
        def __iter__(self):
            raise RuntimeError("boom")

        def keys(self):
            raise RuntimeError("boom")

        def __len__(self):
            raise RuntimeError("boom")

    class Sneaky(str):
        pass

    cases = [deep_dict, {"text": deep_list}, Evil(text="hi"), {"text": Sneaky("hi")}, {"text": b"hi"},
             {"text": "hi", **{f"k{i}": i for i in range(100_000)}}, {"text": "x" * (MAX_ARGS_BYTES + 1)},
             {"text": "hi", "pad": ["x" * 1000] * 100}, {"text": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}},
             {"text": object()}, {"text": float("inf")}, "{\"text\": \"hi\"}", [("text", "hi")], 7, b"{}"]

    async def go():
        for args in cases:
            started = time.perf_counter()
            result = await registry.call("speak", args, APP)
            assert (result.status, result.code) == ("refused", "invalid_arguments"), type(args)
            assert time.perf_counter() - started < 2.0
            assert len(json.dumps(result.to_json())) < 1000  # nothing large goes back to the model

    run(go())
    assert ran == []
    assert all(len(r.getMessage()) < 1000 for r in caplog.records)  # and nothing large goes to the log
    assert len(caplog.records) == len(cases)


def test_unexpected_argument_names_are_shown_five_at_most_with_a_count(caplog):
    registry, _ = boundary_registry()
    caplog.set_level(logging.WARNING, logger="brain.agent.tools")
    result = run(registry.call("stop", {f"k{i:03}": i for i in range(40)}, APP))
    assert result.detail == "unexpected argument(s) ['k000', 'k001', 'k002', 'k003', 'k004'] and 35 more"
    (line,) = [r.getMessage() for r in caplog.records]
    assert "arg_keys=['k000', 'k001', 'k002', 'k003', 'k004'] and 35 more" in line and "k005" not in line


def test_a_handler_cannot_outlast_its_timeout_or_turn_it_into_success():
    """A handler that swallows cancellation and returns late used to come back "ok" after the timeout."""
    registry = custom_registry()
    late = []

    async def stubborn(args, caller):
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            await asyncio.sleep(0.6)
            late.append("finished late")
            return {"done": True}

    registry.register("test_tool", stubborn)

    async def go():
        started = time.perf_counter()
        result = await registry.call("test_tool", {}, APP)
        elapsed = time.perf_counter() - started
        assert (result.status, result.code) == ("unavailable", "timeout")
        assert elapsed < 0.5  # the 0.2 s timeout, not the handler's 0.6 s of cleanup
        await asyncio.sleep(0.8)
        assert late == ["finished late"]  # its late result went nowhere

    run(go())


def test_each_tool_has_its_own_timeout_and_listen_waits_as_long_as_it_was_asked_to():
    registry = ToolRegistry()
    assert registry.timeout_for("stop", {}) == 15.0
    assert registry.timeout_for("listen", {}) == 20.0  # 10 s for the tool plus the default 10 s of listening
    assert registry.timeout_for("listen", {"timeout_s": 60}) == 70.0
    assert registry.timeout_for("listen", {"timeout_s": 2.5}) == 12.5
    assert ToolRegistry(timeout_s=3.0).timeout_for("get_pose", {}) == 3.0


def test_nothing_an_adapter_or_handler_says_reaches_the_model():
    """Only the registry's own words go back: fixed messages, and details it composed itself."""
    registry = custom_registry()
    secret = r"cannot read C:\Users\someone\secret\token.bin (card P9-99, the phone's camera)"

    async def go():
        for exc, code, detail in ((NotYetImplemented(Capability.IMAGE, secret), "not_built", "image"),
                                  (NotSupported(Capability.NAVIGATE, secret), "body_not_supported", "navigate"),
                                  (BodyUnavailable(secret), "body_unavailable", None),
                                  (BodyError(secret, code="bad_frame"), "body_error", "bad_frame"),
                                  (BodyError(secret, code=secret), "body_error", None),  # a code that is not a word
                                  (RuntimeError(secret), "internal", None)):
            async def handler(args, caller, exc=exc):
                raise exc

            registry.register("test_tool", handler)
            result = await registry.call("test_tool", {}, APP)
            assert (result.code, result.detail) == (code, detail)
            said = json.dumps(result.to_json())
            assert "secret" not in said and "P9-99" not in said and "phone" not in said and "C:" not in said

    logging.getLogger("brain.agent.tools").disabled = True
    try:
        run(go())
    finally:
        logging.getLogger("brain.agent.tools").disabled = False


def test_fixed_messages_name_no_card_and_no_particular_body():
    for code, message in MESSAGES.items():
        assert not re.search(r"\bP\d-\d\d\b|\bR-\d\d\b", message), code
        assert not re.search(r"phone|android|robot|sim|adb|laptop|windows", message, re.IGNORECASE), code
        assert message[0] == "I" or message.startswith(("My ", "That ", "Something ")), code


def test_caller_carries_who_is_asking_and_refuses_malformed_identity():
    jeff = Caller(Channel.APP, "confirmed", {"voice", "face", "face"}, "p-jeff")
    assert jeff.person_id == "p-jeff" and jeff.evidence == frozenset({"voice", "face"})
    assert jeff.confirmed_by_face_and_voice and not APP.confirmed_by_face_and_voice
    assert APP.person_id is None and CONSOLE.person_id is None
    for make in (lambda: Caller("App"), lambda: Caller("root"), lambda: Caller(Channel.APP, "CONFIRMED"),
                 lambda: Caller(Channel.APP, "confirmed", "voiceface"),  # a string is not a set of evidence
                 lambda: Caller(Channel.APP, "confirmed", {"voice", "fingerprint"}),
                 lambda: Caller(Channel.APP, person_id="..\\jeff"), lambda: Caller(Channel.APP, person_id=7)):
        with pytest.raises(ValueError):
            make()


def test_a_turns_channel_is_that_of_its_least_privileged_input():
    """The rule P0-07 must follow: the caller comes from where the context's content came from, never
    from the model; anything through the body, the network or a tool makes the turn APP."""
    local, body, remote, tool = (InputSource.LOCAL_CONSOLE, InputSource.BODY, InputSource.REMOTE_CONSOLE,
                                 InputSource.TOOL_RESULT)
    assert turn_channel([local]) is Channel.CONSOLE and turn_channel([local, local]) is Channel.CONSOLE
    for mixed in ([], [body], [local, body], [body, local], [remote], [local, remote], [local, tool],
                  ["local_console", "body"], iter([local, body])):
        assert turn_channel(mixed) is Channel.APP
    with pytest.raises(ValueError):
        turn_channel(["console"])  # an unknown source is an error, never a privilege
    caller = Caller.for_turn([local, body], band="confirmed", evidence={"voice", "face"}, person_id="p-jeff")
    assert caller == Caller(Channel.APP, ConfidenceBand.CONFIRMED, {"voice", "face"}, "p-jeff")
    assert Caller.for_turn([local]) == CONSOLE and Caller.for_turn([]) == APP

    registry = custom_registry(channels=["console"])

    async def handler(args, caller):
        return {"done": True}

    registry.register("test_tool", handler)

    async def go():
        # Jeff typed at the console, but a picture is in the context: the turn is not a console turn.
        mixed = await registry.call("test_tool", {}, Caller.for_turn([local, tool]))
        assert (mixed.status, mixed.code) == ("refused", "channel_not_permitted")
        assert (await registry.call("test_tool", {}, Caller.for_turn([local]))).ok

    run(go())
