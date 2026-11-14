"""Hardening batch 1 (docs/testing/50 section 4.A): one failing-first test per fix.

Each test names the H-item or finding it proves. Larger groups live in their own modules.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from brain.config import REPO_ROOT

BRAIN = REPO_ROOT / "brain"


def run_O(code: str) -> subprocess.CompletedProcess:
    """Run `code` in a child interpreter with `python -O`, which removes every assert."""
    return subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True, cwd=REPO_ROOT,
                          timeout=120)


# ---- H-06: no assert for validation --------------------------------------------------------------------

def test_h06_no_assert_statement_anywhere_under_brain():
    """E2 (T-X01-038): python -O removes assert, so nothing under brain/ may use it."""
    found = [f"{p.relative_to(REPO_ROOT).as_posix()}:{n.lineno}" for p in sorted(BRAIN.rglob("*.py"))
             for n in ast.walk(ast.parse(p.read_text(encoding="utf-8-sig"))) if isinstance(n, ast.Assert)]
    assert found == []


def test_h06_validators_still_refuse_bad_input_under_python_O():
    """T-P0-05-071: the tool registry and the protocol validate with explicit checks, not asserts."""
    code = (
        "import asyncio\n"
        "from brain.agent.tools import ToolRegistry, CONSOLE\n"
        "from brain.transport import protocol as p\n"
        "reg = ToolRegistry()\n"
        "async def go():\n"
        "    out = []\n"
        "    for args in ('text', [1], 7, {'text': float('inf')}, {'nested': {'a': {1: 2}}}):\n"
        "        r = await reg.call('get_pose', args, CONSOLE)\n"
        "        out.append(r.status)\n"
        "    return out\n"
        "print(asyncio.run(go()))\n"
        "for bad in (None, 5, [], 'x', {'v': 0}, {'v': 0, 'type': 'hello'}):\n"
        "    try:\n        p.validate(bad)\n    except p.ProtocolError:\n        pass\n"
        "    else:\n        print('ACCEPTED', bad)\n"
        "try:\n    p.ProtocolError('not_a_code', 'x')\nexcept ValueError:\n    print('REFUSED')\n"
        "else:\n    print('ACCEPTED-CODE')\n"
    )
    out = run_O(code)
    assert out.returncode == 0, out.stderr
    assert "ACCEPTED" not in out.stdout and "REFUSED" in out.stdout, out.stdout
    assert "internal" not in out.stdout.splitlines()[0], out.stdout  # refused, not an internal error


# ---- H-19 and X01-F01: frame and number checks in the protocol layer -------------------------------------

import asyncio  # noqa: E402
import struct  # noqa: E402

from brain.transport import protocol as p  # noqa: E402
from brain.transport.link import Link  # noqa: E402


@pytest.mark.parametrize("text", ['{"a":1e400}', '{"a":-1e400}', '[1e999]', '{"v":0,"payload":{"x":[2e308]}}'])
def test_x01_f01_decode_refuses_numbers_that_overflow_to_infinity(text):
    with pytest.raises(p.ProtocolError) as e:
        p.decode(text)
    assert e.value.code == "bad_json"


def test_x01_f01_finite_extremes_still_decode():
    assert p.decode('{"a":1.7976931348623157e308,"b":-0.0,"c":5e-324}') == {"a": 1.7976931348623157e308, "b": -0.0,
                                                                           "c": 5e-324}


def _frame(*, v=0, code=4, flags=0, msg_id=1, ts=1, data=b"ab", length=None):
    return struct.pack(">BBHIqI", v, code, flags, msg_id, ts, len(data) if length is None else length) + data


def test_h19_unpack_binary_refuses_ts_above_2_53_minus_1_and_id_zero():
    """T-P0-05-072: the frame header can carry an int64; the protocol allows 0 to 2^53-1 and ids from 1."""
    assert p.unpack_binary(_frame(ts=p.MAX_TS)).ts == p.MAX_TS
    for bad in (_frame(ts=p.MAX_TS + 1), _frame(ts=2**63 - 1), _frame(msg_id=0)):
        with pytest.raises(p.ProtocolError) as e:
            p.unpack_binary(bad)
        assert e.value.code == "bad_binary"


@pytest.mark.parametrize("kw", [
    {"msg_id": 0}, {"msg_id": -1}, {"msg_id": 2**32}, {"msg_id": True}, {"msg_id": 1.0}, {"msg_id": "1"},
    {"ts": -1}, {"ts": p.MAX_TS + 1}, {"ts": True}, {"ts": 1.5}, {"ts": None},
    {"data": "text"}, {"data": None}, {"data": 5}, {"data": [1, 2]},
])
def test_h19_pack_binary_refuses_bad_fields_with_a_named_error(kw):
    args = {"msg_id": 1, "ts": 1, "data": b"x"} | kw
    with pytest.raises((p.FrameValueError, p.FrameTypeError)):
        p.pack_binary("echo", args["msg_id"], args["ts"], args["data"])


def test_h19_pack_binary_accepts_the_edges():
    for msg_id, ts in ((1, 0), (p.MAX_ID, p.MAX_TS)):
        assert p.unpack_binary(p.pack_binary("echo", msg_id, ts, b"")).id == msg_id
    assert p.unpack_binary(p.pack_binary("echo", 1, 1, bytearray(b"ab"))).data == b"ab"


class _NullWs:
    sent: list

    def __init__(self):
        self.sent = []

    async def send(self, x):
        self.sent.append(x)


def _send(mtype, payload, **kw):
    async def go():
        link = Link(_NullWs(), p.BRAIN)
        return await link.send(mtype, payload, **kw)
    return asyncio.run(go())


@pytest.mark.parametrize("kw", [{"ts": p.MAX_TS + 1}, {"ts": -1}, {"ts": True}, {"ts": 1.5}, {"ts": "5"}])
@pytest.mark.parametrize("check", [True, False])
def test_h19_link_send_refuses_a_bad_timestamp(kw, check):
    with pytest.raises((p.FrameValueError, p.FrameTypeError)):
        _send("heartbeat", {}, check=check, **kw)


@pytest.mark.parametrize("payload", [None, [], "x", 5, b"x"])
def test_h19_link_send_checks_the_payload_type_before_using_it(payload):
    with pytest.raises(p.FrameTypeError):
        _send("echo", payload, data=b"x")
    with pytest.raises(p.FrameTypeError):
        _send("heartbeat", payload)


@pytest.mark.parametrize("data", ["text", 5, [1], {"a": 1}])
def test_h19_link_send_checks_the_data_type(data):
    with pytest.raises(p.FrameTypeError):
        _send("echo", {"byte_length": 1, "crc32": 0}, data=data)
    with pytest.raises(p.FrameTypeError):
        _send("heartbeat", {}, data=data)


def test_h19_link_send_ts_edge_is_accepted():
    assert _send("heartbeat", {}, ts=p.MAX_TS)["ts_brain"] == p.MAX_TS


# ---- H-18 and X01-F02: command-line ranges --------------------------------------------------------------

from brain import __main__ as cli  # noqa: E402
from brain.limits import BODY_CHECK_WAIT_MAX_S, ECHO_TEST_MAX_BYTES, ECHO_TEST_MAX_COUNT  # noqa: E402

BAD_FLAGS = [
    ("serve", "--port", ["0", "-1", "65536", "99999999999999999999", "abc", "", "1.5", "nan"]),
    ("probe", "--port", ["0", "65536", "x"]),
    ("probe", "--count", ["0", "-1", str(ECHO_TEST_MAX_COUNT + 1), "10000000000", "x", "inf"]),
    ("probe", "--bytes", ["-1", str(ECHO_TEST_MAX_BYTES + 1), "99999999999", "x", "1e3"]),
    ("body-check", "--wait", ["-1", "nan", "inf", "-inf", str(BODY_CHECK_WAIT_MAX_S + 1), "1e308", "x", ""]),
]


@pytest.mark.parametrize("cmd, flag, value", [(c, f, v) for c, f, vs in BAD_FLAGS for v in vs])
def test_h18_bad_numeric_flags_give_a_usage_error_and_exit_2(cmd, flag, value, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([cmd, f"{flag}={value}"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "error:" in err and "Traceback" not in err


@pytest.mark.parametrize("argv", [["serve", "--port", "1"], ["serve", "--port", "65535"], ["probe", "--count", "1"],
                                  ["probe", "--count", "10000"], ["probe", "--bytes", "0"],
                                  ["probe", "--bytes", "1048576"], ["body-check", "--wait", "0"],
                                  ["body-check", "--wait", "3600"]])
def test_h18_the_edges_of_each_range_parse(argv):
    parser = cli.build_parser({})
    parser.parse_args(argv)


def test_h18_info_is_read_only(tmp_path, monkeypatch, capsys):
    """T-P0-04-018: `info` reports; it does not create the data folder."""
    from brain.config import parse_config

    target = tmp_path / "not-yet"
    # data_dir is confined to the config's own folder since H-05: a relative folder under tmp_path.
    config = parse_config({"paths": {"data_dir": "not-yet"}, "network": {}}, base=tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    assert cli.info() == 0
    assert not target.exists()
    assert "not-yet" in capsys.readouterr().out


# ---- H-12: constructor ranges for BrainServer and Link ---------------------------------------------------

from brain.limits import (HEARTBEAT_MS_MAX, HEARTBEAT_MS_MIN, LIVENESS_TIMEOUT_MS_MAX,  # noqa: E402
                          LIVENESS_TIMEOUT_MS_MIN, TOKEN_MIN_BYTES)
from brain.transport.server import BrainServer  # noqa: E402

GOOD_TOKEN = bytes(range(TOKEN_MIN_BYTES))
NAN, INF = float("nan"), float("inf")


def test_h12_token_minimum_is_32_bytes():
    BrainServer(token=GOOD_TOKEN)
    BrainServer(token=GOOD_TOKEN + b"x")
    for n in (0, 1, 16, 31):
        with pytest.raises(ValueError, match="32 bytes"):
            BrainServer(token=bytes(range(n)))


@pytest.mark.parametrize("token", [None, "a" * 40, 5, [1] * 40, bytearray(40)])
def test_h12_token_must_be_bytes(token):
    with pytest.raises((TypeError, ValueError), match="token"):
        BrainServer(token=token)


HB_MIN, HB_MAX = HEARTBEAT_MS_MIN / 1000, HEARTBEAT_MS_MAX / 1000
LV_MIN, LV_MAX = LIVENESS_TIMEOUT_MS_MIN / 1000, LIVENESS_TIMEOUT_MS_MAX / 1000
BAD_TIMING = [
    (0, 3.0), (0.0, 3.0), (-1.0, 3.0), (NAN, 3.0), (INF, 3.0), (True, 3.0), ("1", 3.0), (None, 3.0),
    (HB_MIN / 2, 3.0), (HB_MAX * 2, LV_MAX), (1.0, 1.0), (2.0, 1.0), (1.0, NAN), (1.0, INF), (1.0, 0),
    (1.0, -5.0), (1.0, LV_MAX * 2), (1.0, LV_MIN / 2), (1.0, True), (1.0, None), (HB_MAX, HB_MAX),
]


@pytest.mark.parametrize("hb, lv", BAD_TIMING)
def test_h12_brain_server_refuses_bad_heartbeat_and_liveness(hb, lv):
    with pytest.raises((ValueError, TypeError), match="heartbeat|liveness"):
        BrainServer(token=GOOD_TOKEN, heartbeat_s=hb, liveness_s=lv)


@pytest.mark.parametrize("hb, lv", BAD_TIMING)
def test_h12_link_refuses_bad_heartbeat_and_liveness(hb, lv):
    async def go():
        with pytest.raises((ValueError, TypeError), match="heartbeat|liveness"):
            Link(_NullWs(), p.BODY, heartbeat_s=hb, liveness_s=lv)
    asyncio.run(go())


def test_h12_the_edges_are_accepted():
    for hb, lv in ((HB_MIN, LV_MIN + 0.001), (HB_MIN, 0.3), (HB_MAX, LV_MAX), (2.0, 6.0)):
        BrainServer(token=GOOD_TOKEN, heartbeat_s=hb, liveness_s=lv)


@pytest.mark.parametrize("name", ["hello_timeout_s", "send_timeout_s"])
@pytest.mark.parametrize("value", [0, -1.0, NAN, INF, True, None, 10**9])
def test_h12_timeouts_must_be_positive_finite_and_bounded(name, value):
    with pytest.raises((ValueError, TypeError), match=name):
        BrainServer(token=GOOD_TOKEN, **{name: value})


@pytest.mark.parametrize("port", [-1, 65536, True, "80", None, 1.5])
def test_h12_port_range(port):
    with pytest.raises((ValueError, TypeError), match="port"):
        BrainServer(port=port, token=GOOD_TOKEN)


# ---- H-08: bounded log lines ------------------------------------------------------------------------------

import logging  # noqa: E402

from brain import logsafe  # noqa: E402
from brain.limits import LOG_RECORD_MAX_BYTES  # noqa: E402


def test_h08_short_items_gives_a_count_and_the_first_few_keys():
    text = logsafe.short_items((f"key{i}" for i in range(3000)), 3000)
    assert text.startswith("3000: ['key0', 'key1'") and text.endswith("and 2992 more")
    assert len(text) < 200
    assert logsafe.short_items(["a\nb"], 1) == r"1: ['a\nb']"  # %r form: no raw newline
    assert len(logsafe.short_items(["x" * 10_000], 1)) < 100


def test_h08_cap_filter_cuts_any_record_to_the_limit():
    f = logsafe.CapRecordSize()
    big = logging.LogRecord("x", logging.INFO, "f", 1, "keys %r", (list(range(5000)),), None)
    assert f.filter(big) is True
    assert len(big.getMessage().encode("utf-8")) <= LOG_RECORD_MAX_BYTES
    assert big.getMessage().endswith(logsafe.TRUNCATED)
    wide = logging.LogRecord("x", logging.INFO, "f", 1, "é" * 5000, (), None)  # 2-byte characters
    f.filter(wide)
    assert len(wide.getMessage().encode("utf-8")) <= LOG_RECORD_MAX_BYTES
    short = logging.LogRecord("x", logging.INFO, "f", 1, "ok %s", ("fine",), None)
    f.filter(short)
    assert short.getMessage() == "ok fine"
    broken = logging.LogRecord("x", logging.INFO, "f", 1, "%d", ("not a number",), None)
    assert f.filter(broken) is True  # a bad format string does not break logging


def test_h08_setup_logging_puts_the_cap_on_every_handler(tmp_path, monkeypatch):
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(cli, "data_dir", lambda config: tmp_path)
    try:
        cli.setup_logging({}, "t")
        added = [h for h in root.handlers if h not in before]
        assert added and all(any(isinstance(x, logsafe.CapRecordSize) for x in h.filters) for h in added)
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_h08_peer_events_and_hello_lists_give_short_log_lines(caplog):
    """T-P0-05-047 / T-X01-034: 700 event keys, a 64-name command list and a large echo_test_result each give
    one short line, in the server and in the body adapter. (700, not 3000: since H-04 an event over
    EVENT_MAX_BYTES, 16 KiB, is refused as too_large before it is logged; 700 keys are still about 10 KB,
    five times LOG_RECORD_MAX_BYTES.)"""
    from fakes import body, run, with_server

    async def go(server, url):
        async with body(url, commands=tuple(f"cmd_{i}_" + "x" * 30 for i in range(64)),
                        capabilities=("event", "echo")) as c:
            await c.link.send("event", {"name": "sensor", "data": {f"k{i}": i for i in range(700)}})
            await c.link.send("event", {"name": "echo_test_result",
                                        "data": {f"k{i}": "v" * 8 for i in range(700)}})
            await asyncio.sleep(0.3)
    caplog.set_level(logging.DEBUG)
    run(with_server(go))
    sizes = [len(r.getMessage().encode("utf-8")) for r in caplog.records if r.levelno >= logging.INFO]
    assert sizes and max(sizes) <= LOG_RECORD_MAX_BYTES, max(sizes)
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO)
    assert "700" in text and "k699" not in text and "too_large" not in text


# ---- H-20: handlers that ignore cancellation ----------------------------------------------------------------

from brain.agent.tools import CONSOLE, ToolRegistry  # noqa: E402
from brain.limits import ABANDONED_TASKS_PER_TOOL_MAX  # noqa: E402


def _stubborn_registry(hold_s: float, started: list):
    reg = ToolRegistry(timeout_s=0.05)

    async def stubborn(args, caller):
        started.append(1)
        end = asyncio.get_running_loop().time() + hold_s
        while asyncio.get_running_loop().time() < end:
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass  # ignores cancellation
        return {}
    reg.register("who_is_present", stubborn)
    return reg


def test_h20_abandoned_handlers_are_capped_and_the_tool_is_then_unavailable():
    """T-P0-06-062: beyond ABANDONED_TASKS_PER_TOOL_MAX stuck handlers the tool answers `unavailable` at once,
    without starting another handler; it works again when the stuck ones have ended."""
    started: list = []

    async def go():
        reg = _stubborn_registry(0.8, started)
        results = [await reg.call("who_is_present", {}, CONSOLE) for _ in range(ABANDONED_TASKS_PER_TOOL_MAX + 3)]
        assert len(reg._abandoned) <= ABANDONED_TASKS_PER_TOOL_MAX
        codes = [r.code for r in results]
        assert codes[:ABANDONED_TASKS_PER_TOOL_MAX + 1] == ["timeout"] * (ABANDONED_TASKS_PER_TOOL_MAX + 1)
        assert codes[ABANDONED_TASKS_PER_TOOL_MAX + 1:] == ["tool_stuck"] * 2
        assert all(r.status == "unavailable" for r in results)
        assert len(started) == ABANDONED_TASKS_PER_TOOL_MAX + 1  # no handler started after the tool was shut off
        await asyncio.sleep(1.0)  # the stuck handlers end
        again = await reg.call("who_is_present", {}, CONSOLE)
        assert again.code == "timeout"  # allowed to run again (and times out again: the handler is stubborn)
    asyncio.run(go())


def test_h20_concurrent_timeouts_never_hold_more_than_the_cap():
    async def go():
        reg = _stubborn_registry(0.4, [])
        rs = await asyncio.gather(*(reg.call("who_is_present", {}, CONSOLE)
                                    for _ in range(ABANDONED_TASKS_PER_TOOL_MAX + 6)))
        assert {r.code for r in rs} == {"timeout"}
        assert len(reg._abandoned) <= ABANDONED_TASKS_PER_TOOL_MAX
        await asyncio.sleep(0.6)
    asyncio.run(go())


# ---- H-24: broad and silent excepts need a reviewed marker -----------------------------------------------------

from tools.chaos import lints  # noqa: E402


def _problems(tmp_path, source):
    f = tmp_path / "m.py"
    f.write_text(source, encoding="utf-8")
    return lints.except_problems([tmp_path])


def test_h24_brain_has_no_unmarked_broad_or_silent_except():
    """T-X01-038: every `except Exception`, `except BaseException`, bare `except` and `except ...: pass` under
    brain/ carries `# broad-except: <reason>` (or `# swallowed: <reason>` for a silent narrow one)."""
    assert lints.except_problems() == []


@pytest.mark.parametrize("source", [
    "try:\n    x()\nexcept Exception:\n    y()\n",
    "try:\n    x()\nexcept Exception as e:\n    y(e)\n",
    "try:\n    x()\nexcept BaseException:\n    raise\n",
    "try:\n    x()\nexcept:\n    y()\n",
    "try:\n    x()\nexcept (OSError, Exception):\n    y()\n",
    "try:\n    x()\nexcept OSError:\n    pass\n",
    "try:\n    x()\nexcept OSError:\n    ...\n",
    "try:\n    x()\nexcept OSError:\n    'docstring only'\n",
    "try:\n    x()\nexcept Exception:  # broad-except:\n    y()\n",  # a marker needs a reason
    "try:\n    x()\nexcept Exception:  # broad-except: ok\n    y()\n",  # and a real one
    "try:\n    x()\nexcept OSError:  # broad-except: wrong marker for a silent handler\n    pass\n",
])
def test_h24_the_lint_catches_unmarked_handlers(tmp_path, source):
    assert len(_problems(tmp_path, source)) == 1


@pytest.mark.parametrize("source", [
    "try:\n    x()\nexcept Exception:  # broad-except: a listener bug must not end the session\n    y()\n",
    "try:\n    x()\n# broad-except: a listener bug must not end the session\nexcept Exception:\n    y()\n",
    "try:\n    x()\nexcept:  # broad-except: nothing may escape this cleanup path\n    y()\n",
    "try:\n    x()\nexcept OSError:  # swallowed: the folder may already be gone\n    pass\n",
    "try:\n    x()\nexcept (ValueError, OSError):\n    y()\n",
    "try:\n    x()\nexcept OSError:\n    log()\n",
])
def test_h24_marked_or_narrow_handlers_pass(tmp_path, source):
    assert _problems(tmp_path, source) == []


# ---- H-31: heading range is (-pi, pi] -----------------------------------------------------------------------------

import math  # noqa: E402
import random  # noqa: E402

from brain.body.types import Pose, TrackingState, quaternion_from_heading, wrap_angle_rad  # noqa: E402


def _heading(angle: float) -> float | None:
    return Pose("f", (0.0, 0.0, 0.0), quaternion_from_heading(angle), TrackingState.OK, 1).heading_rad


def test_h31_the_exact_boundary_maps_to_plus_pi():
    """T-P0-06-074: quaternion_from_heading(-pi) used to give heading exactly -pi."""
    assert _heading(-math.pi) == math.pi
    assert _heading(math.pi) == math.pi
    assert wrap_angle_rad(-math.pi) == math.pi and wrap_angle_rad(math.pi) == math.pi
    assert wrap_angle_rad(0.0) == 0.0 and wrap_angle_rad(2 * math.pi) == pytest.approx(0.0, abs=1e-12)
    assert wrap_angle_rad(3 * math.pi) == math.pi and wrap_angle_rad(-3 * math.pi) == math.pi


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_h31_wrap_refuses_non_finite_angles(bad):
    with pytest.raises(ValueError):
        wrap_angle_rad(bad)


def test_h31_every_heading_is_in_minus_pi_exclusive_to_pi_inclusive_over_100000_angles():
    rng = random.Random(31)
    edges = [0.0, math.pi, -math.pi, math.nextafter(math.pi, 0), math.nextafter(-math.pi, 0),
             math.nextafter(math.pi, 4), math.nextafter(-math.pi, -4), math.pi / 2, -math.pi / 2,
             2 * math.pi, -2 * math.pi, 1e-12, -1e-12, 3.0, -3.0, 3.14, -3.14]
    angles = edges + [rng.uniform(-2 * math.pi, 2 * math.pi) for _ in range(100_000)]
    for a in angles:
        h = _heading(a)
        assert h is not None and -math.pi < h <= math.pi, (a, h)
        diff = math.remainder(h - a, 2 * math.pi)  # the same direction, modulo a full turn
        assert abs(diff) < 1e-9, (a, h)


# ---- H-13: a missing mypy fails; it does not skip -----------------------------------------------------------------

from _pytest.outcomes import Failed, Skipped  # noqa: E402
from test_typing import require_tool  # noqa: E402


def test_h13_a_missing_tool_fails_unless_explicitly_allowed():
    """T-P0-04-038: the type check cannot pass by not running."""
    with pytest.raises(Failed, match="not installed"):
        require_tool("mypy", find=lambda name: None, env={})
    with pytest.raises(Failed):
        require_tool("mypy", find=lambda name: None, env={"EA_ALLOW_MISSING_TOOLS": "0"})
    with pytest.raises(Failed):
        require_tool("mypy", find=lambda name: None, env={"EA_ALLOW_MISSING_TOOLS": "true"})
    with pytest.raises(Skipped):
        require_tool("mypy", find=lambda name: None, env={"EA_ALLOW_MISSING_TOOLS": "1"})
    require_tool("mypy", find=lambda name: object(), env={})  # installed: returns


def test_h13_no_other_test_skips_for_a_missing_tool():
    """The skip-if-missing pattern is not copied anywhere else (a missing check must never pass)."""
    offenders = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        if path.name in ("test_typing.py", "test_hardening_batch1.py"):
            continue
        text = path.read_text(encoding="utf-8-sig")
        if "find_spec" in text and "skip" in text:
            offenders.append(path.name)
    assert offenders == []
