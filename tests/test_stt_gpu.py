"""Card P2-04, GPU half: CUDA DLL registration, the self-test, device and compute-type resolution, the warm-up and the
fall back to the CPU. Fast and offline: a fake faster-whisper model and a fake ctranslate2, no GPU needed (the real GPU
is tests/test_stt_gpu_real.py, behind --run-real-model). docs/stt.md, "The GPU".
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from stt_helpers import FakeWhisperModel, factory_for, feed_all, make_model, seg, symlink_or_skip, tone

from brain import obs
from brain.config import SttConfig
from brain.limits import STT_CUDA_PROBE_TIMEOUT_S, STT_GPU_REASON_MAX_CHARS, STT_WARM_AUDIO_S, STT_WARM_LEVEL, STT_WARM_TIMEOUT_S
from brain.obs.health import HealthBoard
from brain.voice import stt as S
from brain.voice.stt import (LocalWhisperEngine, SttError, SttGpuError, SttGpuOutOfMemory, SttUnavailable,
                             resolve_compute_type)

pytestmark = pytest.mark.trace("T-P2-04-007", "T-P2-04-013")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Events:
    """Collects events (per-type limits reset first)."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def __enter__(self) -> Events:
        obs.LOG.reset_limits()
        obs.LOG.add_sink(self.items.append)
        return self

    def __exit__(self, *exc: object) -> None:
        obs.LOG.remove_sink(self.items.append)

    def phases(self) -> list[str]:
        return [x["f"]["phase"] for x in self.items if x["type"] == "speech.stt" and x["f"].get("phase")]

    def of(self, phase: str) -> list[dict[str, Any]]:
        return [x["f"] for x in self.items if x["type"] == "speech.stt" and x["f"].get("phase") == phase]


class GpuModel(FakeWhisperModel):
    """A fake model whose call number `fail_on` (1 and 2 are the warm-up's two passes, 3 the first utterance) raises `error`; `crash_iter_on` raises while iterating."""

    def __init__(self, segments: Any = None, *, fail_on: int | None = None, error: BaseException | None = None,
                 crash_iter_on: int | None = None, **kw: Any) -> None:
        super().__init__(segments, **kw)
        self.fail_on, self.err, self.crash_iter_on, self.n = fail_on, error, crash_iter_on, 0

    def transcribe(self, audio: Any, **options: Any) -> tuple[Any, Any]:
        self.n += 1
        if self.fail_on == self.n and self.err is not None:
            self.calls.append(options)
            raise self.err
        if self.crash_iter_on == self.n and self.err is not None:
            self.crash = self.err
        else:
            self.crash = None
        return super().transcribe(audio, **options)


def gpu_factory(gpu: Any, cpu: Any | None = None, *, fail_gpu: BaseException | None = None) -> Any:
    made: list[tuple[Any, ...]] = []
    cpu_model = cpu or FakeWhisperModel([seg("cpu words")])

    def factory(folder: Path, device: str, compute_type: str, cpu_threads: int) -> Any:
        made.append((folder, device, compute_type, cpu_threads))
        if device == "cuda":
            if fail_gpu is not None:
                raise fail_gpu
            return gpu
        return cpu_model

    factory.made = made  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def models(tmp_path: Path) -> Path:
    make_model(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_cuda_cache() -> Any:
    S._CUDA_RESULT.clear()
    yield
    S._CUDA_RESULT.clear()


def engine_for(models: Path, factory: Any, *, usable: tuple[bool, str] | None = (True, "pretend"), monkeypatch: Any = None,
               **cfg: Any) -> LocalWhisperEngine:
    if usable is not None:
        monkeypatch.setattr(S, "cuda_usable", lambda folder, ct: usable)
    e = LocalWhisperEngine(SttConfig(model_dir=models, **cfg), factory=factory)
    return e


# ---- CUDA DLL folders ---------------------------------------------------------------------------------------------------


def _wheels(site: Path, *names: str, with_bin: bool = True) -> None:
    for n in names:
        d = site / "nvidia" / n
        (d / "bin" if with_bin else d).mkdir(parents=True)
        if with_bin:
            (d / "bin" / f"{n}64_12.dll").write_bytes(b"MZ")


def test_only_existing_nvidia_bin_folders_under_site_packages_are_found(tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    _wheels(site, "cublas", "cudnn", "cuda_nvrtc")
    _wheels(site, "no_bin", with_bin=False)
    (site / "nvidia" / "a_file").write_bytes(b"x")
    (site / "nvidia" / "file_bin").mkdir()
    (site / "nvidia" / "file_bin" / "bin").write_bytes(b"not a folder")
    found = S.cuda_dll_dirs([site])
    assert [Path(f).parent.name for f in found] == ["cublas", "cuda_nvrtc", "cudnn"]
    assert all(Path(f).is_dir() and Path(f).name == "bin" for f in found)


@pytest.mark.parametrize("make", ["missing", "empty_nvidia", "nvidia_is_a_file"])
def test_no_wheels_means_no_folders(tmp_path: Path, make: str) -> None:
    site = tmp_path / "s"
    if make == "empty_nvidia":
        (site / "nvidia").mkdir(parents=True)
    elif make == "nvidia_is_a_file":
        site.mkdir()
        (site / "nvidia").write_bytes(b"x")
    assert S.cuda_dll_dirs([site]) == [] and S.cuda_dll_dirs([]) == []


def test_a_link_that_leads_out_of_site_packages_is_ignored(tmp_path: Path) -> None:
    site, outside = tmp_path / "site", tmp_path / "outside"
    (outside / "bin").mkdir(parents=True)
    (site / "nvidia").mkdir(parents=True)
    symlink_or_skip(site / "nvidia" / "evil", outside)
    assert S.cuda_dll_dirs([site]) == []


def test_the_default_search_is_this_interpreters_own_site_packages() -> None:
    import sysconfig

    own = {Path(sysconfig.get_paths()[k]) for k in ("purelib", "platlib")}
    assert set(S._site_packages()) <= own and S._site_packages()


def test_registration_adds_each_folder_once_and_puts_them_first_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    added: list[str] = []
    monkeypatch.setattr(S, "_DLL_DIRS", None)
    monkeypatch.setattr(S.sys, "platform", "win32")
    monkeypatch.setattr(S, "cuda_dll_dirs", lambda site_dirs=None: ["C:\\v\\cublas\\bin", "C:\\v\\cudnn\\bin"])
    monkeypatch.setattr(os, "add_dll_directory", lambda d: added.append(d), raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join(["C:\\Windows", "C:\\v\\cudnn\\bin", "C:\\tools"]))
    first = S.register_cuda_dlls()
    second = S.register_cuda_dlls()
    assert first == second == ["C:\\v\\cublas\\bin", "C:\\v\\cudnn\\bin"] and added == first  # idempotent: added only once
    path = os.environ["PATH"].split(os.pathsep)
    assert path[:2] == first and path.count("C:\\v\\cudnn\\bin") == 1 and path[2:] == ["C:\\Windows", "C:\\tools"]


def test_registration_skips_a_folder_the_system_refuses_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def add(d: str) -> None:
        if "bad" in d:
            raise OSError("no")

    monkeypatch.setattr(S, "_DLL_DIRS", None)
    monkeypatch.setattr(S.sys, "platform", "win32")
    monkeypatch.setattr(S, "cuda_dll_dirs", lambda site_dirs=None: ["C:\\bad\\bin", "C:\\good\\bin"])
    monkeypatch.setattr(os, "add_dll_directory", add, raising=False)
    assert S.register_cuda_dlls() == ["C:\\good\\bin"]


def test_registration_never_raises_even_when_the_search_itself_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(site_dirs: Any = None) -> list[str]:
        raise RuntimeError("weird")

    monkeypatch.setattr(S, "_DLL_DIRS", None)
    monkeypatch.setattr(S.sys, "platform", "win32")
    monkeypatch.setattr(S, "cuda_dll_dirs", boom)
    assert S.register_cuda_dlls() == []


def test_registration_does_nothing_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(S, "_DLL_DIRS", None)
    monkeypatch.setattr(S.sys, "platform", "linux")
    monkeypatch.setattr(S, "cuda_dll_dirs", lambda site_dirs=None: called.append("search") or ["x"])  # type: ignore[func-returns-value]
    assert S.register_cuda_dlls() == [] and called == []


def test_the_default_factory_registers_dlls_for_the_gpu_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(S, "register_cuda_dlls", lambda: calls.append("reg") or [])  # type: ignore[func-returns-value]

    class Whisper:
        def __init__(self, path: str, **kw: Any) -> None:
            calls.append(f"load {kw['device']} {kw['compute_type']} {kw['cpu_threads']}")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Whisper))
    S._default_factory(tmp_path, "cpu", "int8", 4)
    S._default_factory(tmp_path, "cuda", "int8_float16", 0)
    assert calls == ["load cpu int8 4", "reg", "load cuda int8_float16 0"]


def test_the_self_test_script_registers_the_dlls_itself_and_is_valid_python() -> None:
    compile(S._CUDA_SELFTEST, "selftest", "exec")
    assert "add_dll_directory" in S._CUDA_SELFTEST and "sys.argv[3]" in S._CUDA_SELFTEST and "vad_filter=False" in S._CUDA_SELFTEST


# ---- compute type resolution ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("device,asked,expected", [
    ("cpu", "int8", "int8"), ("cpu", "float32", "float32"), ("cpu", "float16", "int8"), ("cpu", "int8_float16", "int8"),
    ("cuda", "int8", "int8_float16"), ("cuda", "int8_float16", "int8_float16"), ("cuda", "float16", "float16"),
    ("cuda", "float32", "float32"),
    ("cpu", "bogus", "int8"), ("cuda", "bogus", "int8_float16"), ("cpu", "", "int8"), ("cuda", "", "int8_float16")])
def test_compute_type_resolution_table(device: str, asked: str, expected: str) -> None:
    assert resolve_compute_type(device, asked)[0] == expected


def test_the_cpu_never_gets_a_float16_type_and_the_notes_say_why() -> None:
    for asked in ("float16", "int8_float16"):
        t, note = resolve_compute_type("cpu", asked)
        assert t == "int8" and "needs a GPU" in note
    assert "int8_float16" in resolve_compute_type("cuda", "int8")[1]
    assert resolve_compute_type("cuda", "float16")[1] == "" and resolve_compute_type("cpu", "int8")[1] == ""


# ---- the self-test (cuda_usable) ----------------------------------------------------------------------------------------


def _fake_ct2(monkeypatch: pytest.MonkeyPatch, count: Any) -> None:
    def n() -> int:
        if isinstance(count, BaseException):
            raise count
        return int(count)

    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(get_cuda_device_count=n))


def _child(monkeypatch: pytest.MonkeyPatch, *, code: int = 0, out: str = "CUDA_SELFTEST_OK\n", raises: BaseException | None = None) -> list[Any]:
    seen: list[Any] = []

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        seen.append((cmd, kw))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")

    monkeypatch.setattr(S.subprocess, "run", fake_run)
    return seen


def test_a_passing_self_test_runs_in_a_child_with_a_timeout_and_the_dll_folders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_ct2(monkeypatch, 1)
    monkeypatch.setattr(S, "register_cuda_dlls", lambda: ["C:\\a\\bin", "C:\\b\\bin"])
    seen = _child(monkeypatch)
    assert S.cuda_usable(tmp_path, "int8") == (True, "CUDA self-test passed")
    cmd, kw = seen[0]
    assert cmd[1:3] == ["-I", "-c"] and cmd[4] == str(tmp_path) and cmd[5] == "int8_float16"  # the resolved GPU compute type
    assert cmd[6] == os.pathsep.join(["C:\\a\\bin", "C:\\b\\bin"])
    assert kw["timeout"] == STT_CUDA_PROBE_TIMEOUT_S and kw["stdin"] is subprocess.DEVNULL and kw["env"]["HF_HUB_OFFLINE"] == "1"


def test_the_self_test_result_is_cached_for_the_life_of_the_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_ct2(monkeypatch, 1)
    seen = _child(monkeypatch)
    for _ in range(5):
        assert S.cuda_usable(tmp_path, "int8")[0] and S.cuda_usable(tmp_path, "int8_float16")[0]  # int8 and int8_float16 are one key
    assert len(seen) == 1
    S.cuda_usable(tmp_path, "float16")  # another compute type is another test
    assert len(seen) == 2


@pytest.mark.parametrize("kw,expect", [
    ({"code": 3221225477}, "failed (exit code 3221225477)"),
    ({"code": 1, "out": "CUDA_SELFTEST_FAIL RuntimeError: CUDA failed with error out of memory\n"}, "out of memory"),
    ({"code": 0, "out": "something else\n"}, "failed"),
    ({"raises": subprocess.TimeoutExpired("x", 90)}, "timed out"),
    ({"raises": FileNotFoundError("python")}, "could not run (FileNotFoundError)"),
    ({"raises": subprocess.SubprocessError("x")}, "could not run")])
def test_a_failing_crashing_or_hanging_self_test_means_no_gpu_with_a_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                            kw: dict[str, Any], expect: str) -> None:
    _fake_ct2(monkeypatch, 1)
    _child(monkeypatch, **kw)
    ok, why = S.cuda_usable(tmp_path, "int8")
    assert not ok and expect in why and len(why) < 200


def test_the_self_test_reason_is_clean_and_short(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_ct2(monkeypatch, 1)
    _child(monkeypatch, code=1, out="CUDA_SELFTEST_FAIL RuntimeError: " + "x\x1b[31m\u202e" * 500 + "\n")
    why = S.cuda_usable(tmp_path, "int8")[1]
    assert "\x1b" not in why and "\u202e" not in why and len(why) < STT_GPU_REASON_MAX_CHARS + 60


@pytest.mark.parametrize("count,expect", [(0, "no CUDA device"), (-1, "no CUDA device"), (RuntimeError("driver"), "could not be queried"),
                                         (OSError("dll"), "OSError"), ("x", "could not be queried")])
def test_no_device_or_a_query_failure_means_cpu_and_no_child_is_started(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                                        count: Any, expect: str) -> None:
    _fake_ct2(monkeypatch, count)
    seen = _child(monkeypatch)
    ok, why = S.cuda_usable(tmp_path, "int8")
    assert not ok and expect in why and seen == []


# ---- the engine: choosing the device -------------------------------------------------------------------------------------


def test_auto_with_a_usable_gpu_loads_int8_float16_with_no_cpu_threads_and_warms_up_first(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gpu = GpuModel([seg("gpu words")])
    f = gpu_factory(gpu)
    e = engine_for(models, f, monkeypatch=monkeypatch, device="auto", cpu_threads=8)
    with Events() as ev:
        feed_all(e, "a", tone(1.0))
        r = run(e.finish())
    assert r.text == "gpu words" and f.made[0][1:] == ("cuda", "int8_float16", 0)  # cpu_threads is for the CPU only
    assert e.device == "cuda" and e.compute_used == "int8_float16" and "int8_float16" in e.device_used and "pretend" in e.device_used
    warm_n = int(STT_WARM_AUDIO_S * 16000)
    assert len(gpu.calls) == 3 and gpu.audio_lengths == [warm_n, warm_n, 16000]
    assert [c["vad_filter"] for c in gpu.calls] == [False, True, True]  # the warm-up runs the model, then loads the VAD
    assert e.warm_state == "up" and e.warm_ms is not None and e.warm_ms >= 0
    assert ev.phases() == ["device", "warming", "warm"] and ev.of("warm")[0]["warm_ms"] >= 0
    assert ev.of("device")[0]["compute"] == "int8_float16" and ev.of("warming")[0]["device"] == "cuda"
    e.close()


def test_the_warm_up_audio_is_one_second_of_quiet_noise() -> None:
    a = LocalWhisperEngine._warm_audio()
    assert a.dtype.name == "float32" and len(a) == int(STT_WARM_AUDIO_S * 16000)
    assert 0 < float(abs(a).max()) < 10 * STT_WARM_LEVEL and float(abs(a).max()) < 0.1


@pytest.mark.parametrize("asked,expected", [("int8", "int8_float16"), ("float16", "float16"), ("int8_float16", "int8_float16"),
                                            ("float32", "float32")])
def test_gpu_compute_type_follows_the_config(models: Path, monkeypatch: pytest.MonkeyPatch, asked: str, expected: str) -> None:
    f = gpu_factory(GpuModel())
    e = engine_for(models, f, monkeypatch=monkeypatch, device="cuda", compute_type=asked)
    feed_all(e, "a", tone(1.0))
    run(e.finish())
    assert f.made[0][1:] == ("cuda", expected, 0)
    e.close()


@pytest.mark.parametrize("device", ["cpu", "auto"])
@pytest.mark.parametrize("asked,expected", [("int8", "int8"), ("float32", "float32"), ("float16", "int8"), ("int8_float16", "int8")])
def test_on_the_cpu_a_float16_type_becomes_int8_and_cpu_threads_are_used(models: Path, monkeypatch: pytest.MonkeyPatch, device: str,
                                                                          asked: str, expected: str) -> None:
    f = gpu_factory(GpuModel())
    e = engine_for(models, f, monkeypatch=monkeypatch, usable=(False, "no GPU here"), device=device, compute_type=asked, cpu_threads=6)
    feed_all(e, "a", tone(1.0))
    assert run(e.finish()).text == "cpu words"
    assert f.made == [(models / "base.en", "cpu", expected, 6)]
    e.close()


def test_auto_with_an_unusable_gpu_falls_back_to_cpu_with_an_event_and_a_health_reason(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board = HealthBoard()
    f = gpu_factory(GpuModel())
    e = engine_for(models, f, monkeypatch=monkeypatch, usable=(False, "the CUDA self-test failed: cublas64_12.dll not found"), device="auto")
    with Events() as ev:
        feed_all(e, "a", tone(1.0))
        assert run(e.finish()).text == "cpu words"
    assert f.made[0][1] == "cpu" and "GPU unusable: the CUDA self-test failed" in e.device_used and "using CPU" in e.device_used
    dev = ev.of("device")
    assert len(dev) == 1 and dev[0]["fallback"] is True and dev[0]["device"] == "cpu" and "using CPU" in dev[0]["note"]
    assert "warming" not in ev.phases()  # no automatic warm-up on the CPU
    for x in ev.items:
        board.on_event(x)
    src = board.sources["speech_vendors"]
    assert src.state == "up" and "GPU unusable" in src.reason and "using CPU" in src.reason
    e.close()


def test_the_health_reason_after_a_cpu_fall_back_is_there_before_any_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    board = HealthBoard()
    board.on_event({"type": "speech.stt", "f": {"phase": "device", "ok": True, "fallback": True, "device": "cpu",
                                                "note": "GPU unusable: no CUDA device, using CPU"}})
    s = board.sources["speech_vendors"]
    assert s.state == "degraded" and "GPU unusable" in s.reason


def test_explicit_cuda_with_an_unusable_gpu_is_unavailable_with_the_reason_and_the_health_source_shows_it(models: Path,
                                                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    board = HealthBoard()
    f = gpu_factory(GpuModel())
    e = engine_for(models, f, monkeypatch=monkeypatch, usable=(False, "no CUDA device is usable"), device="cuda")
    with Events() as ev:
        for i in range(3):
            feed_all(e, f"u{i}", tone(1.0))
            with pytest.raises(SttUnavailable, match="device = cuda.*no CUDA device is usable"):
                run(e.finish())
    assert f.made == []  # it did not quietly use the CPU
    for x in ev.items:
        board.on_event(x)
    src = board.sources["speech_vendors"]
    assert src.state == "down" and "GPU unusable" in src.reason
    e.close()


def test_explicit_cuda_whose_load_fails_is_unavailable_not_a_crash(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = gpu_factory(GpuModel(), fail_gpu=RuntimeError("cublas64_12.dll is not found"))
    e = engine_for(models, f, monkeypatch=monkeypatch, device="cuda")
    feed_all(e, "a", tone(1.0))
    with pytest.raises(SttUnavailable, match="could not be loaded.*GPU"):
        run(e.finish())
    assert [m[1] for m in f.made] == ["cuda"]
    e.close()


def test_auto_whose_gpu_load_fails_uses_the_cpu_with_the_reason(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = gpu_factory(GpuModel(), fail_gpu=RuntimeError("cublas64_12.dll is not found"))
    e = engine_for(models, f, monkeypatch=monkeypatch, device="auto", cpu_threads=3)
    feed_all(e, "a", tone(1.0))
    assert run(e.finish()).text == "cpu words"
    assert [m[1:] for m in f.made] == [("cuda", "int8_float16", 0), ("cpu", "int8", 3)]
    assert "GPU unusable: the load failed (RuntimeError)" in e.device_used
    e.close()


def test_a_missing_model_file_is_still_refused_before_any_gpu_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probed: list[Any] = []
    monkeypatch.setattr(S, "cuda_usable", lambda folder, ct: probed.append(folder) or (True, "x"))  # type: ignore[func-returns-value]
    e = LocalWhisperEngine(SttConfig(model_dir=tmp_path / "nowhere", device="auto"), factory=gpu_factory(GpuModel()))
    feed_all(e, "a", tone(1.0))
    with pytest.raises(SttUnavailable):
        run(e.finish())
    assert probed == []
    e.close()


# ---- failures during a decode ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("error,named", [
    (RuntimeError("CUDA failed with error out of memory"), SttGpuOutOfMemory), (MemoryError(), SttGpuOutOfMemory),
    (RuntimeError("cudaMalloc failed: out of memory"), SttGpuOutOfMemory), (RuntimeError("Failed to allocate 2147483648 bytes"), SttGpuOutOfMemory),
    (RuntimeError("CUDA failed with error an illegal memory access was encountered"), SttGpuError),
    (RuntimeError("cuDNN error: CUDNN_STATUS_EXECUTION_FAILED"), SttGpuError), (RuntimeError("cublas error 13"), SttGpuError),
    (RuntimeError("CUDA driver version is insufficient"), SttGpuError), (OSError("the GPU device lost after a driver reset"), SttGpuError),
    (RuntimeError("CUDA failed with error unknown error"), SttGpuError)])
@pytest.mark.parametrize("where", ["call", "iteration"])
def test_a_gpu_error_during_a_decode_fails_that_decode_by_name_and_later_decodes_use_the_cpu_once(
        models: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException, named: type[SttError], where: str) -> None:
    board = HealthBoard()
    gpu = GpuModel(fail_on=3, error=error) if where == "call" else GpuModel(crash_iter_on=3, error=error)
    f = gpu_factory(gpu)
    e = engine_for(models, f, monkeypatch=monkeypatch, device="auto", cpu_threads=5)
    with Events() as ev:
        feed_all(e, "u1", tone(1.0))
        with pytest.raises(named) as exc:
            run(e.finish())
        assert type(exc.value) is named and "CPU" in str(exc.value) and len(str(exc.value)) < 200
        for i in (2, 3):
            feed_all(e, f"u{i}", tone(1.0))
            assert run(e.finish()).text == "cpu words"
    assert [m[1:] for m in f.made] == [("cuda", "int8_float16", 0), ("cpu", "int8", 5)]  # one fall back, then the CPU keeps going
    assert e.device == "cpu" and "GPU failed" in e.device_used and gpu.n == 3
    fb = ev.of("fallback")
    assert len(fb) == 1 and fb[0]["ok"] is False and "using CPU" in fb[0]["note"] and fb[0]["gpu_fallback"] is True
    failed = [x["f"] for x in ev.items if x["type"] == "speech.stt" and not x["f"].get("phase") and not x["f"]["ok"]]
    assert [x["code"] for x in failed] == [named.__name__]
    for x in ev.items:
        board.on_event(x)
    src = board.sources["speech_vendors"]
    assert src.state == "degraded" and "GPU failed" in src.reason  # the later good transcripts keep the reason: still degraded
    e.close()


def test_the_gpu_error_does_not_take_the_caller_down_and_the_next_decode_after_it_is_fine_even_when_queued(models: Path,
                                                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    gpu = GpuModel(fail_on=3, error=RuntimeError("CUDA failed with error out of memory"))
    e = engine_for(models, gpu_factory(gpu), monkeypatch=monkeypatch, device="auto")

    async def go() -> list[str]:
        out = []
        for i in range(4):
            feed_all(e, f"q{i}", tone(0.6))
            try:
                out.append((await e.finish()).text)
            except SttGpuOutOfMemory:
                out.append("oom")
        return out

    assert run(go()) == ["oom", "cpu words", "cpu words", "cpu words"]
    e.close()


def test_an_unrelated_error_on_the_gpu_is_not_taken_for_a_gpu_failure(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gpu = GpuModel(fail_on=3, error=ValueError("bad segment"))
    f = gpu_factory(gpu)
    e = engine_for(models, f, monkeypatch=monkeypatch, device="auto")
    feed_all(e, "a", tone(1.0))
    with pytest.raises(SttUnavailable, match="failed"):
        run(e.finish())
    assert e.device == "cuda" and [m[1] for m in f.made] == ["cuda"]  # no fall back
    e.close()


def test_a_gpu_error_while_there_is_no_gpu_in_use_is_an_ordinary_failure(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    e = engine_for(models, gpu_factory(GpuModel(), cpu=FakeWhisperModel(error=RuntimeError("CUDA failed with error out of memory"))),
                   monkeypatch=monkeypatch, usable=(False, "none"), device="auto")
    feed_all(e, "a", tone(1.0))
    with pytest.raises(SttUnavailable):  # on the CPU a message is just a message: no named GPU error, no second fall back
        run(e.finish())
    e.close()


def test_a_gpu_error_in_the_warm_up_falls_back_before_the_first_utterance_and_the_caller_sees_nothing(models: Path,
                                                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    gpu = GpuModel(fail_on=1, error=RuntimeError("CUDA failed with error out of memory"))
    f = gpu_factory(gpu)
    e = engine_for(models, f, monkeypatch=monkeypatch, device="auto")
    with Events() as ev:
        feed_all(e, "a", tone(1.0))
        assert run(e.finish()).text == "cpu words"
    assert [m[1] for m in f.made] == ["cuda", "cpu"] and e.device == "cpu" and ev.of("fallback")
    assert "during the warm-up" in e.device_used
    e.close()


# ---- the warm-up ------------------------------------------------------------------------------------------------------------


def test_warm_loads_warms_once_and_returns_the_milliseconds(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gpu = GpuModel(delay_s=0.0)
    e = engine_for(models, gpu_factory(gpu), monkeypatch=monkeypatch, device="auto")
    ms = e.warm()
    assert ms is not None and ms >= 0 and e.warm_state == "up" and e.loaded and len(gpu.calls) == 2
    assert e.warm() == ms and len(gpu.calls) == 2  # once (two passes: the model, then the voice-activity model)
    feed_all(e, "a", tone(1.0))
    run(e.finish())
    assert len(gpu.calls) == 3  # no second warm-up before the first utterance
    e.close()


def test_warm_works_on_the_cpu_when_asked_but_is_not_automatic_there(models: Path) -> None:
    model = FakeWhisperModel()
    e = LocalWhisperEngine(SttConfig(model_dir=models), factory=factory_for(model))
    assert e.warm() is not None and [c["vad_filter"] for c in model.calls] == [False, True]
    e.close()


def test_warm_failure_is_none_an_event_and_a_degraded_health_reason_and_decoding_still_works(models: Path,
                                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    board = HealthBoard()
    gpu = GpuModel(fail_on=1, error=RuntimeError("something odd in the warm-up"))
    e = engine_for(models, gpu_factory(gpu), monkeypatch=monkeypatch, device="auto")
    with Events() as ev:
        assert e.warm() is None
        assert e.warm_state == "failed" and e.warm_ms is None
        feed_all(e, "a", tone(1.0))
        assert run(e.finish()).text == "hello world"  # not a GPU error: still the GPU model, just not warm
    w = ev.of("warm")
    assert len(w) == 1 and w[0]["ok"] is False and w[0]["code"] == "RuntimeError"
    board.on_event(ev.items[[x["f"].get("phase") for x in ev.items].index("warm")])
    assert board.sources["speech_vendors"].state == "degraded" and "warm-up failed" in board.sources["speech_vendors"].reason
    e.close()


def test_warm_that_hangs_gives_up_after_its_bound_and_never_blocks_longer(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gpu = GpuModel(delay_s=30.0)
    f = gpu_factory(gpu)
    monkeypatch.setattr(S, "cuda_usable", lambda folder, ct: (True, "pretend"))
    e = LocalWhisperEngine(SttConfig(model_dir=models, device="auto"), factory=f, warm_timeout_s=0.2)
    t0 = time.perf_counter()
    with Events() as ev:
        assert e.warm() is None
    assert time.perf_counter() - t0 < 3 and e.warm_state == "failed" and ev.of("warm")[0]["code"] == "SttTimeout"
    gpu.release.set()
    e.close()


def test_warm_never_raises_for_a_model_that_cannot_load_or_a_closed_engine(tmp_path: Path, models: Path) -> None:
    e = LocalWhisperEngine(SttConfig(model_dir=tmp_path / "nowhere"), factory=factory_for())
    assert e.warm() is None and e.warm() is None  # the second is answered from the remembered failure
    e.close()
    assert e.warm() is None
    bad = LocalWhisperEngine(SttConfig(model_dir=models), factory=factory_for(fail=RuntimeError("cannot load")))
    assert bad.warm() is None
    bad.close()
    boom = LocalWhisperEngine(SttConfig(model_dir=models), factory=factory_for(fail=MemoryError()))
    assert boom.warm() is None
    boom.close()


def test_warm_async_keeps_the_event_loop_running(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gpu = GpuModel(delay_s=0.5)
    e = engine_for(models, gpu_factory(gpu), monkeypatch=monkeypatch, device="auto")

    async def go() -> tuple[int, float | None]:
        task = asyncio.create_task(e.warm_async())
        ticks, t0 = 0, time.perf_counter()
        while not task.done():
            await asyncio.sleep(0.01)
            ticks += 1
            if time.perf_counter() - t0 > 0.2:
                gpu.release.set()
        return ticks, await task

    ticks, ms = run(go())
    assert ticks >= 5 and ms is not None
    e.close()


def test_the_first_utterance_waits_for_a_running_warm_up_and_nothing_is_decoded_twice_at_once(models: Path,
                                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    gpu = GpuModel(delay_s=0.3)
    e = engine_for(models, gpu_factory(gpu), monkeypatch=monkeypatch, device="auto")

    async def go() -> str:
        warm = asyncio.create_task(e.warm_async())
        await asyncio.sleep(0.05)
        feed_all(e, "a", tone(1.0))
        text = (await e.finish()).text
        await warm
        return text

    assert run(go()) == "hello world" and gpu.n == 3  # the two warm-up passes and the utterance, one after the other
    e.close()


def test_the_health_source_shows_warming_then_up(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board = HealthBoard()
    states: list[tuple[str, str]] = []
    e = engine_for(models, gpu_factory(GpuModel()), monkeypatch=monkeypatch, device="auto")

    def sink(ev: Any) -> None:
        board.on_event(ev)
        if ev["type"] == "speech.stt":
            s = board.sources["speech_vendors"]
            states.append((s.state, s.reason))

    obs.LOG.reset_limits()
    obs.LOG.add_sink(sink)
    try:
        assert e.warm() is not None
    finally:
        obs.LOG.remove_sink(sink)
    assert [s for s, _ in states] == ["unknown", "unknown", "up"]
    assert "loaded" in states[0][1] and "warming" in states[1][1] and "warm on cuda" in states[2][1]
    e.close()


# ---- the first-use time bound ----------------------------------------------------------------------------------------------


def test_first_use_on_the_gpu_gets_the_time_for_the_self_test_and_warm_up(models: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    real_wait_for = asyncio.wait_for

    async def spy(fut: Any, timeout: float) -> Any:
        seen.append(timeout)
        return await real_wait_for(fut, timeout)

    monkeypatch.setattr(S.asyncio, "wait_for", spy)
    e = engine_for(models, gpu_factory(GpuModel()), monkeypatch=monkeypatch, device="auto")
    feed_all(e, "a", tone(1.0))
    run(e.finish())
    feed_all(e, "b", tone(1.0))
    run(e.finish())
    assert seen[0] >= STT_CUDA_PROBE_TIMEOUT_S + STT_WARM_TIMEOUT_S and seen[1] == e._decode_timeout_s  # only the first use
    e.close()


def test_the_new_limits_are_named_positive_and_cover_the_measured_numbers() -> None:
    assert STT_WARM_TIMEOUT_S > 7.3 * 3 and STT_WARM_AUDIO_S >= 1.0 and 0 < STT_WARM_LEVEL <= 0.05 and STT_GPU_REASON_MAX_CHARS >= 60
