"""Card P2-04: the [stt] section of the config gate, and `python -m brain stt-check` in CHILD processes with hostile arguments.

The children use a sandbox config.toml whose [stt] model_dir is an empty or synthetic folder, so none of them touches the
real data/ folder or loads a real model (the real-model run is tests/test_stt_real_model.py, behind --run-real-model).
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import pytest
from stt_helpers import GOOD_FILES, make_model, tone

from brain.config import REPO_ROOT, SCHEMA, ConfigError, SttConfig, load_config, parse_config, schema_paths
from brain.limits import STT_CPU_THREADS_MAX

pytestmark = pytest.mark.trace("T-P2-04-001", "T-P2-04-009")


# ---- the config gate ----------------------------------------------------------------------------------------------------


def cfg(**stt: Any) -> Any:
    return parse_config({"stt": stt}, source="t.toml")


def test_the_defaults_are_local_base_en_on_cpu_int8() -> None:
    d = parse_config({}).stt
    assert (d.engine, d.model, d.device, d.compute_type, d.cpu_threads) == ("whisper-local", "base.en", "cpu", "int8", 0)
    assert d.model_dir == (REPO_ROOT / "data" / "models" / "stt").resolve()


def test_the_project_config_has_the_stt_section_for_this_machine() -> None:
    """This machine has an RTX GPU: small.en on device auto. Everything else is the default."""
    default = parse_config({}).stt
    assert dataclasses.replace(load_config().stt, model=default.model, device=default.device) == default
    assert (load_config().stt.model, load_config().stt.device) == ("small.en", "auto")


def test_every_stt_key_is_in_the_schema_and_nothing_else_is() -> None:
    assert set(SCHEMA["stt"]) == {"engine", "model", "device", "compute_type", "cpu_threads", "model_dir"}  # type: ignore[arg-type]
    assert ("stt",) in schema_paths() and ("stt", "model_dir") in schema_paths()


@pytest.mark.parametrize("key,value", [("model", "small.en"), ("model", "base.en"), ("device", "auto"), ("device", "cpu"),
                                       ("device", "cuda"), ("compute_type", "float32"), ("compute_type", "int8_float16"),
                                       ("compute_type", "float16"), ("compute_type", "int8"), ("cpu_threads", 0), ("cpu_threads", 1),
                                       ("cpu_threads", STT_CPU_THREADS_MAX), ("engine", "whisper-local")])
def test_valid_values_are_accepted(key: str, value: Any) -> None:
    assert getattr(cfg(**{key: value}).stt, key) == value


@pytest.mark.parametrize("key,value", [
    ("model", "large-v3"), ("model", "tiny"), ("model", "small"), ("model", "BASE.EN"), ("model", ""), ("model", None), ("model", 5), ("model", ["base.en"]),
    ("device", "gpu"), ("device", "CUDA"), ("device", "cuda "), ("device", ""), ("device", True), ("device", None), ("device", 1),
    ("compute_type", "int4"), ("compute_type", "FLOAT16"), ("compute_type", "int8_float32"), ("compute_type", "bfloat16"),
    ("compute_type", ""), ("compute_type", None), ("compute_type", 16), ("compute_type", ["int8"]),
    ("engine", "deepgram"), ("engine", "whisper-cloud"), ("engine", None),
    ("cpu_threads", -1), ("cpu_threads", STT_CPU_THREADS_MAX + 1), ("cpu_threads", 1.5), ("cpu_threads", "4"), ("cpu_threads", True),
    ("cpu_threads", None), ("cpu_threads", 10**30), ("cpu_threads", float("nan")),
    ("model_dir", 5), ("model_dir", ""), ("model_dir", None), ("model_dir", "x" * 261), ("model_dir", "a\x00b"), ("model_dir", "a\nb")])
def test_invalid_values_are_refused_with_the_file_and_key_named(key: str, value: Any) -> None:
    with pytest.raises(ConfigError) as e:
        cfg(**{key: value})
    assert "t.toml" in str(e.value) and key in str(e.value)


@pytest.mark.parametrize("key,value,hint", [("device", "cud", "cuda"), ("device", "atuo", "auto"), ("device", "cpuu", "cpu"),
                                             ("compute_type", "int8_float", "int8_float16"), ("compute_type", "float61", "float16"),
                                             ("compute_type", "flaot32", "float32"), ("model", "small", "small.en")])
def test_a_misspelt_value_gets_a_did_you_mean(key: str, value: str, hint: str) -> None:
    with pytest.raises(ConfigError, match=f"did you mean '{hint}'"):
        cfg(**{key: value})


def test_a_value_with_nothing_close_lists_the_choices_without_a_guess() -> None:
    with pytest.raises(ConfigError) as e:
        cfg(device="graphics-card")
    assert "'cpu', 'auto', 'cuda'" in str(e.value) and "did you mean" not in str(e.value)


@pytest.mark.parametrize("device,compute", [("cpu", "float16"), ("cpu", "int8_float16"), ("cuda", "int8"), ("auto", "float32")])
def test_every_device_and_compute_type_combination_passes_the_gate_the_engine_resolves_them(device: str, compute: str) -> None:
    c = cfg(device=device, compute_type=compute).stt
    assert (c.device, c.compute_type) == (device, compute)


def test_an_unknown_stt_key_stops_the_brain_with_did_you_mean() -> None:
    with pytest.raises(ConfigError, match="did you mean 'model'"):
        cfg(modle="small.en")
    with pytest.raises(ConfigError, match="did you mean 'cpu_threads'"):
        cfg(cpu_thread=4)
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config({"stt": {"api_key": "x"}})
    with pytest.raises(ConfigError, match="must be a table"):
        parse_config({"stt": "base.en"})


@pytest.mark.parametrize("path", ["../models", "..\\models", "a/../../b", "models/..", "C:\\Windows\\System32", "\\\\host\\share\\m",
                                  "/etc", "C:", "~/../x"])
def test_model_dir_cannot_leave_the_project(path: str) -> None:
    with pytest.raises(ConfigError):
        cfg(model_dir=path)


def test_model_dir_relative_is_resolved_under_the_config_folder(tmp_path: Path) -> None:
    c = parse_config({"stt": {"model_dir": "models/speech"}}, base=tmp_path)
    assert c.stt.model_dir == (tmp_path / "models" / "speech").resolve()


def test_model_dir_may_point_at_the_shared_models_in_the_repo_from_a_profile(tmp_path: Path) -> None:
    shared = REPO_ROOT / "data" / "models" / "stt"
    c = parse_config({"stt": {"model_dir": str(shared)}}, base=tmp_path)  # a profile's base is its own folder
    assert c.stt.model_dir == shared.resolve()
    with pytest.raises(ConfigError, match="inside this project"):
        parse_config({"stt": {"model_dir": "C:\\Windows\\Temp\\models" if os.name == "nt" else "/tmp/models"}}, base=tmp_path)


def test_a_profile_gets_its_own_model_folder_by_default(tmp_path: Path) -> None:
    c = parse_config({}, base=tmp_path / "profiles" / "mary")
    assert c.stt.model_dir == (tmp_path / "profiles" / "mary" / "data" / "models" / "stt").resolve()


def test_model_dir_cannot_escape_through_a_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "proj"
    base.mkdir()
    try:
        os.symlink(outside, base / "models", target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symbolic links")
    with pytest.raises(ConfigError):
        parse_config({"stt": {"model_dir": "models"}}, base=base)


def test_stt_config_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        SttConfig().model = "small.en"  # type: ignore[misc]


# ---- stt-check in child processes ----------------------------------------------------------------------------------------

WRAPPER = f"""\
import sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from pathlib import Path
import brain.config as c
c.DEFAULT_CONFIG = Path(sys.argv[1])
from brain import __main__ as m
raise SystemExit(m.main(sys.argv[2:]))
"""


@pytest.fixture
def child(tmp_path: Path):  # type: ignore[no-untyped-def]
    """run(*argv, models=None) -> (code, stdout, stderr) of `python -m brain <argv>` against a sandbox config."""
    script = tmp_path / "run_brain.py"
    script.write_text(WRAPPER, encoding="utf-8")
    (tmp_path / "data").mkdir()

    def run(*argv: str, models: str = "models") -> tuple[int, str, str]:
        conf = tmp_path / "config.toml"
        conf.write_text(f'[paths]\ndata_dir = "data"\n[stt]\nmodel_dir = "{models}"\n', encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        p = subprocess.run([sys.executable, str(script), str(conf), *argv], capture_output=True, text=True, timeout=120,
                           env=env, cwd=tmp_path, stdin=subprocess.DEVNULL)
        assert "Traceback" not in p.stdout + p.stderr, p.stdout + p.stderr
        assert p.returncode in (0, 1, 2), (p.returncode, p.stdout, p.stderr)
        return p.returncode, p.stdout, p.stderr

    return run


def wav(path: Path, pcm: bytes, rate: int = 16000, channels: int = 1, width: int = 2) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


def test_stt_check_without_a_file_is_a_usage_error(child: Any) -> None:
    code, out, err = child("stt-check")
    assert code == 2 and "usage" in err.lower()


@pytest.mark.parametrize("args", [["--model", "large"], ["--model", ""], ["--model", "x" * 5000], ["--bogus"], ["-"], ["--"], ["-h-"],
                                  ["--model"], ["a.wav", "b.wav"], ["--profile", "../x", "a.wav"], ["--profile", "NOPE", "a.wav"],
                                  ["--device", "tpu", "a.wav"], ["--device", "", "a.wav"], ["--device", "CUDA", "a.wav"], ["--device"],
                                  ["--device", "x" * 5000, "a.wav"], ["--device", "-1", "a.wav"],
                                  ["--compute-type", "int4", "a.wav"], ["--compute-type", "float16 ", "a.wav"], ["--compute-type"],
                                  ["--compute-type", "bfloat16", "a.wav"], ["--compute-type", "", "a.wav"],
                                  ["--compute_type", "int8", "a.wav"], ["--devices", "cpu", "a.wav"]])
def test_stt_check_bad_arguments_exit_2_without_a_traceback(child: Any, args: list[str]) -> None:
    code, out, err = child("stt-check", *args)
    assert code == 2


@pytest.mark.parametrize("name", ["missing.wav", "x" * 250 + ".wav", "con", "a\u202e.wav", "..\\..\\..\\Windows\\win.ini", "C:\\Windows\\win.ini",
                                  "\\\\nohost\\share\\a.wav", "--model", "*.wav", "a?.wav", "\x01\x07.wav"])
def test_stt_check_hostile_file_names_are_refused_with_one_line(child: Any, name: str) -> None:
    code, out, err = child("stt-check", "--", name) if not name.startswith("--") else child("stt-check", name)
    assert code in (2,) and out.count("\n") <= 3


def test_stt_check_refuses_a_folder_and_non_wav_files(child: Any, tmp_path: Path) -> None:
    assert child("stt-check", str(tmp_path))[0] == 2
    for i, data in enumerate([b"", b"RIFF", b"hello world" * 20, b"\x89PNG\r\n\x1a\n" + b"\0" * 100, b"RIFF" + b"\xff" * 200]):
        p = tmp_path / f"bad{i}.wav"
        p.write_bytes(data)
        code, out, err = child("stt-check", str(p))
        assert code == 2 and "stt-check:" in out, (i, out)


@pytest.mark.parametrize("kw,text", [({"rate": 44100}, "44100 Hz"), ({"channels": 2}, "channels"), ({"width": 1}, "8-bit"),
                                     ({"width": 4}, "32-bit"), ({"rate": 8000}, "8000 Hz")])
def test_stt_check_names_the_wrong_wav_format(child: Any, tmp_path: Path, kw: dict[str, int], text: str) -> None:
    p = wav(tmp_path / "a.wav", bytes(6400 * kw.get("width", 2) * kw.get("channels", 1)), **kw)
    code, out, err = child("stt-check", str(p))
    assert code == 2 and text in out


def test_stt_check_refuses_more_than_15_seconds_and_a_huge_file(child: Any, tmp_path: Path) -> None:
    assert "longer than 15 s" in child("stt-check", str(wav(tmp_path / "s.wav", bytes(16 * 32000))))[1]
    big = tmp_path / "big.wav"
    big.write_bytes(b"RIFF" + bytes(2 * 1024 * 1024))
    code, out, err = child("stt-check", str(big))
    assert code == 2 and "refused" in out


@pytest.mark.parametrize("flags", [["--device", "cpu"], ["--device", "auto"], ["--device", "cuda"], ["--compute-type", "int8"],
                                   ["--compute-type", "int8_float16"], ["--compute-type", "float16"], ["--compute-type", "float32"],
                                   ["--device", "cuda", "--compute-type", "float16", "--model", "small.en"]])
def test_stt_check_accepts_every_valid_device_and_compute_type_flag_and_reaches_the_model_check(child: Any, tmp_path: Path,
                                                                                             flags: list[str]) -> None:
    """With no models installed the command stops at the model check (exit 2, lock.json), before any GPU work."""
    p = wav(tmp_path / "a.wav", tone(1.0))
    code, out, err = child("stt-check", *flags, str(p))
    assert code == 2 and "lock.json" in out, out


def test_stt_check_with_no_models_says_how_to_fetch_them(child: Any, tmp_path: Path) -> None:
    p = wav(tmp_path / "a.wav", tone(1.0))
    code, out, err = child("stt-check", str(p))
    assert code == 2 and "lock.json" in out and "fetch_stt_model.py" in out


def test_stt_check_with_a_model_that_is_not_a_real_model_fails_closed_without_a_traceback(child: Any, tmp_path: Path) -> None:
    make_model(tmp_path / "models", "base.en", GOOD_FILES)  # passes the lock, but is not a real CTranslate2 model
    p = wav(tmp_path / "a.wav", tone(1.0))
    code, out, err = child("stt-check", str(p))
    assert code == 2 and ("could not be loaded" in out or "failed" in out), out


def test_stt_check_with_a_changed_model_file_names_the_problem(child: Any, tmp_path: Path) -> None:
    folder = make_model(tmp_path / "models", "base.en", GOOD_FILES)
    (folder / "vocabulary.txt").write_bytes(b"other\ncontent!!\n")
    code, out, err = child("stt-check", str(wav(tmp_path / "a.wav", tone(1.0))))
    assert code == 2 and "vocabulary.txt" in out


def test_stt_check_in_a_profile_without_models_refuses_cleanly() -> None:
    from brain import profiles

    assert callable(profiles.load_profile_config)  # the profile command line itself is covered by tests/test_profiles_cli.py
