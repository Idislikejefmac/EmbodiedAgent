"""The chaos runner (plan card X-01 step 2): `python -m brain chaos --quick | --full [--seed N]`.

Order of a run:
1. Preflight, fail closed: the egress guard must be on and allow no outside destination at all.
2. Static stage: the manifest loads; the drift check passes; every experiment names real entries.
3. Cases: for each seed, each experiment (sorted), each class, the generated cases in order. Each case
   runs in a fresh event loop in its own thread, under every monitor, with a timeout, inside the
   sandbox. Same seed, same cases, same verdicts.
4. Report: failures with their seed and a one-line reproduction; counts; the verdict on each of the
   seven invariants; entry points no experiment covers; results to data/chaos/<date>-<commit>.json.

Exit codes: 0 every invariant held (expected failures of recorded defects allowed); 1 something
failed (including a recorded defect that now passes: remove its marker); 2 the harness could not run
(bad arguments, unreadable manifest, unsafe environment).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fnmatch
import hashlib
import importlib
import json
import pkgutil
import random
import reprlib
import sys
import threading
import time
import traceback
import tracemalloc
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from brain import egress
from brain.config import REPO_ROOT
from brain.limits import PEER_LOG_LINES_PER_MIN_MAX

from . import lints, monitors, sandbox
from . import manifest as manifest_mod
from . import thresholds as T
from .api import CLASS_ORDER, INVARIANTS, Case, CaseClass, Ctx, Experiment, Gen, Violation, na_reason, registered

RESULTS_DIR = REPO_ROOT / "data" / "chaos"
_short = reprlib.Repr()
_short.maxlevel, _short.maxstring, _short.maxother, _short.maxlist, _short.maxdict = 3, 80, 80, 6, 6


class HarnessError(Exception):
    """The harness itself cannot run as asked (exit code 2)."""


def derive_seed(*parts: object) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(digest[:16], 16) & T.SEED_MAX


def short(value: Any, limit: int = 300) -> str:
    try:
        text = _short.repr(value)
    except Exception as e:  # broad-except: describing a hostile object must not crash the report
        text = f"<unrepresentable {type(value).__name__}: {type(e).__name__}>"
    return text[:limit]


def load_cases() -> dict[str, Experiment]:
    """Import every module in tools/chaos/cases (each registers its experiments) and return them."""
    from . import cases

    for info in sorted(pkgutil.iter_modules(cases.__path__), key=lambda i: i.name):
        importlib.import_module(f"{cases.__name__}.{info.name}")
    return registered()


# ---- results -------------------------------------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    name: str
    cls: str
    tags: list[str]
    seed: int
    status: str  # pass | fail | xfail | xpass | timeout | aborted
    violations: list[tuple[str, str]] = field(default_factory=list)
    known_defect: str | None = None
    duration_s: float = 0.0
    output: str = ""
    notes: list[str] = field(default_factory=list)
    repro: str = ""

    @property
    def failed(self) -> bool:
        return self.status in ("fail", "xpass", "timeout", "aborted")


@dataclass
class RunReport:
    mode: str
    seeds: list[int]
    plant: str | None
    started: str
    commit: str
    duration_s: float = 0.0
    static: list[str] = field(default_factory=list)
    results: list[CaseResult] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    not_applicable: dict[str, str] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    harness_problems: list[str] = field(default_factory=list)
    results_file: str = ""
    canaries: dict[str, str] = field(default_factory=dict, repr=False)  # for the results scan; never written

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if r.failed]

    @property
    def ok(self) -> bool:
        return not self.failures and not self.static and not self.harness_problems

    def counts(self) -> dict[str, int]:
        return dict(Counter(r.status for r in self.results))

    def invariants(self) -> dict[str, dict[str, int | str]]:
        out: dict[str, dict[str, int | str]] = {}
        for inv in INVARIANTS:
            fails = sum(1 for r in self.results if r.status in ("fail", "timeout") for v in r.violations if v[0] == inv)
            xfails = sum(1 for r in self.results if r.status == "xfail" for v in r.violations if v[0] == inv)
            out[inv] = {"verdict": "held" if not fails else "violated", "violations": fails,
                        "expected_violations_of_recorded_defects": xfails}
        return out

    def to_json(self) -> dict[str, Any]:
        per_exp: dict[str, Counter[str]] = {}
        for r in self.results:
            per_exp.setdefault(r.case_id.split("/")[0], Counter())[r.status] += 1
        return {
            "schema": 1, "date": self.started, "commit": self.commit, "mode": self.mode, "seeds": self.seeds,
            "plant": self.plant, "duration_s": round(self.duration_s, 3), "ok": self.ok, "counts": self.counts(),
            "invariants": self.invariants(), "static": self.static, "harness_problems": self.harness_problems,
            "uncovered_entries": self.uncovered, "not_applicable": self.not_applicable,
            "case_list_digest": self.digests, "experiments": {k: dict(v) for k, v in sorted(per_exp.items())},
            "failures": [asdict(r) for r in self.failures],
            "expected_failures": [{"case_id": r.case_id, "known_defect": r.known_defect, "seed": r.seed,
                                   "violations": r.violations} for r in self.results if r.status == "xfail"],
        }


def git_commit() -> str:
    """The checked-out commit, read from .git without starting a process."""
    git = REPO_ROOT / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="ascii").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            p = git / ref
            if p.exists():
                return p.read_text(encoding="ascii").strip()[:7]
            for line in (git / "packed-refs").read_text(encoding="ascii").splitlines():
                if line.endswith(" " + ref):
                    return line[:7]
            return "unknown"
        return head[:7]
    except OSError:
        return "unknown"


# ---- running one case ------------------------------------------------------------------------------------


def _policy(sb: sandbox.Sandbox, exp: Experiment, allow_real_data: bool, allow_real_phone: bool
            ) -> monitors.SideEffectPolicy:
    procs = set(exp.allow_processes) | ({"adb", "adb.exe"} if allow_real_phone else set())
    return monitors.SideEffectPolicy.make(write_roots=[sb.root], secret_roots=[sb.secrets_dir],
                                          protected_roots=[] if allow_real_data else sandbox.protected_roots(),
                                          allow_processes=procs)


def run_case(exp: Experiment, case: Case, case_id: str, case_seed: int, sb: sandbox.Sandbox, mode: str,
             policy: monitors.SideEffectPolicy) -> CaseResult:
    timeout = case.timeout_s or (T.CHAOS_CASE_TIMEOUT_QUICK_S if mode == "quick" else T.CHAOS_CASE_TIMEOUT_FULL_S)
    mon = monitors.CaseMonitors(policy=policy, canaries=dict(sb.canaries),
                                max_log_lines=case.max_log_lines or PEER_LOG_LINES_PER_MIN_MAX,
                                allow_isolated_traceback=case.isolated_fault)
    ctx = Ctx(rng=random.Random(case_seed), mode=mode, case_seed=case_seed, sandbox=sb, monitors=mon)
    holder: dict[str, Any] = {}

    def target() -> None:
        try:
            if exp.is_async:
                async def wrapper() -> Any:
                    asyncio.get_running_loop().set_exception_handler(mon.loop_exception)
                    out = await exp.check(case, ctx)
                    me = asyncio.current_task()
                    holder["leaked"] = sum(1 for t in asyncio.all_tasks() if t is not me and not t.done())
                    return out

                holder["out"] = asyncio.run(wrapper())
            else:
                holder["out"] = exp.check(case, ctx)
        except Violation as v:
            holder["violation"] = v
        except BaseException as e:  # broad-except: the crash detector; it reports, never re-raises
            holder["crash"] = e
            holder["trace"] = traceback.format_exception(e)[-3:]

    mon.start()
    t0 = time.monotonic()
    thread = threading.Thread(target=target, name=f"chaos-{case_id}", daemon=True)
    thread.start()
    thread.join(timeout)
    duration = time.monotonic() - t0
    timed_out = thread.is_alive()
    violations = list(mon.finish())
    if "violation" in holder:
        violations.insert(0, holder["violation"])
    crash = holder.get("crash")
    if crash is not None and not isinstance(crash, monitors.ChaosBlocked):
        where = "".join(holder.get("trace", [])).strip().splitlines()
        violations.insert(0, Violation("no_crash", f"{type(crash).__name__}: {str(crash)[:200]} "
                                                   f"[{where[-1][:160] if where else ''}]"))
    if holder.get("leaked", 0) > T.CASE_LEAKED_TASKS_MAX:
        violations.append(Violation("bounded_resources", f"{holder['leaked']} tasks still pending after the case"))
    output = short(holder.get("out")) if "out" in holder else ""
    texts = [output] + [v.message for v in violations] + [str(crash) if crash else ""] + ctx.notes
    leaked = monitors.scan_secrets("\n".join(texts), sb.canaries)
    if leaked:
        violations.append(Violation("no_secret_in_output", f"canary secret(s) {leaked} in the case's output or errors"))
    if timed_out:
        status = "timeout"
        violations.insert(0, Violation("bounded_resources", f"still running after {timeout:g} s (case timeout)"))
    else:
        status = "fail" if violations else "pass"
    if case.known_defect:
        status = "xfail" if status in ("fail", "timeout") else "xpass"
    result = CaseResult(case_id, case.name, case.cls.value, sorted(case.tags), case_seed, status,
                        [(v.invariant, _mask(v.message, sb.canaries)) for v in violations], case.known_defect,
                        round(duration, 4), _mask(output, sb.canaries), [_mask(n, sb.canaries) for n in ctx.notes])
    return result


def _mask(text: str, canaries: dict[str, str]) -> str:
    for label, secret in canaries.items():
        if secret:
            text = text.replace(secret, f"<canary:{label}>").replace(secret.upper(), f"<canary:{label}>")
    return text


# ---- generation ------------------------------------------------------------------------------------------


def generate(exp: Experiment, seed: int, mode: str) -> list[tuple[str, Case, int]]:
    """(case id, case, case seed) for one experiment and one seed, in a fixed order."""
    out: list[tuple[str, Case, int]] = []
    for cls in CLASS_ORDER:
        gen_fn = exp.generators[cls]
        rng = random.Random(derive_seed(seed, exp.id, cls.value))
        cases = list(gen_fn(Gen(rng=rng, mode=mode, seed=seed)))
        if not cases and not na_reason(gen_fn):
            raise HarnessError(f"{exp.id}: the {cls.value} generator made no cases "
                               "(use not_applicable('why') if the class does not apply)")
        names: set[str] = set()
        for i, case in enumerate(cases):
            if not isinstance(case, Case) or case.cls is not cls:
                raise HarnessError(f"{exp.id}: the {cls.value} generator yielded {short(case, 80)}, "
                                   f"not a {cls.value} Case")
            if case.name in names:
                raise HarnessError(f"{exp.id}/{cls.value}: case name {case.name!r} used twice")
            names.add(case.name)
            out.append((f"{exp.id}/{cls.value}/{i}", case, derive_seed(seed, exp.id, cls.value, i)))
    return out


def digest(cases: list[tuple[str, Case, int]]) -> str:
    h = hashlib.sha256()
    for case_id, case, case_seed in cases:
        h.update(f"{case_id}|{case.name}|{case_seed}|{sorted(case.tags)}|{short(case.data, 200)}\n".encode())
    return h.hexdigest()[:16]


def _selected(case_id: str, only: list[str]) -> bool:
    return not only or any(case_id == o or case_id.startswith(o.rstrip("/") + "/") or fnmatch.fnmatch(case_id, o)
                           for o in only)


# ---- the run ---------------------------------------------------------------------------------------------


def preflight() -> None:
    policy = egress._policy
    if policy is None:
        raise HarnessError("the egress guard is not on; run the harness as `python -m brain chaos` (or under pytest)")
    if policy.allowed:
        raise HarnessError("the egress guard allows outside destinations "
                           f"({sorted(policy.allowed)}); the harness runs only under a loopback-only guard")


def static_checks(exps: dict[str, Experiment]) -> tuple[list[str], list[str]]:
    """(problems, uncovered entry ids). Problems fail the run."""
    problems: list[str] = []
    m = manifest_mod.load_manifest()
    problems += manifest_mod.drift(m).lines()
    new, stale = lints.assert_problems()
    problems += [f"E2 {line}" for line in new + stale]
    known = m.all_ids()
    covered: set[str] = set()
    for exp in exps.values():
        for pattern in exp.entries:
            hits = {i for i in known if i == pattern or fnmatch.fnmatchcase(i, pattern)}
            if not hits:
                problems.append(f"experiment {exp.id} names {pattern!r}, which matches no manifest entry")
            covered |= hits
    return problems, sorted(known - covered)


@contextlib.contextmanager
def _sandboxed(parent: Path | None) -> Iterator[sandbox.Sandbox]:
    sb = sandbox.create(parent)
    sandbox.enter(sb)
    try:
        yield sb
    finally:
        sandbox.leave(sb)
        sandbox.destroy(sb)


def run(mode: str = "quick", seeds: list[int] | None = None, only: list[str] | None = None,
        plant: str | None = None, *, allow_real_data: bool = False, allow_real_phone: bool = False,
        strict_coverage: bool = False, sandbox_parent: Path | None = None,
        progress: Callable[[str], None] | None = None) -> RunReport:
    if mode not in ("quick", "full"):
        raise HarnessError(f"mode must be quick or full, not {mode!r}")
    seeds = list(seeds or (T.QUICK_SEEDS if mode == "quick" else T.FULL_SEEDS))
    preflight()
    report = RunReport(mode, seeds, plant, datetime.now().isoformat(timespec="seconds"), git_commit())
    start = time.monotonic()
    planted_ctx: contextlib.AbstractContextManager[Any] = contextlib.nullcontext()
    if plant:
        from . import planted

        planted_ctx = planted.active(plant)
    exps = load_cases()
    trace = T.TRACE_MEMORY_QUICK if mode == "quick" else True
    started_tracing = trace and not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start(1)
    try:
        with planted_ctx, _sandboxed(sandbox_parent) as sb:
            report.canaries = dict(sb.canaries)
            report.static, report.uncovered = static_checks(exps)
            if strict_coverage and report.uncovered:
                report.static.append(f"{len(report.uncovered)} manifest entries have no experiment (--strict-coverage)")
            for exp in exps.values():
                for cls in CLASS_ORDER:
                    reason = na_reason(exp.generators[cls])
                    if reason:
                        report.not_applicable[f"{exp.id}/{cls.value}"] = reason
            for seed in seeds:
                for exp in exps.values():
                    cases = generate(exp, seed, mode)
                    report.digests[f"{seed}:{exp.id}"] = digest(cases)
                    policy = _policy(sb, exp, allow_real_data, allow_real_phone)
                    failures = 0
                    for case_id, case, case_seed in cases:
                        if not _selected(case_id, only or []):
                            continue
                        if failures >= exp.abort_after_failures:
                            r = CaseResult(case_id, case.name, case.cls.value, sorted(case.tags), case_seed, "aborted",
                                           [("no_crash", f"not run: {exp.id} hit its abort condition "
                                                         f"({exp.abort_after_failures} failures)")])
                        else:
                            r = run_case(exp, case, case_id, case_seed, sb, mode, policy)
                        r.repro = repro_command(seed, case_id, mode, plant)
                        if r.status in ("fail", "timeout", "xpass"):
                            failures += 1
                        report.results.append(r)
                        if progress and r.failed:
                            progress(format_failure(r))
    finally:
        if started_tracing:
            tracemalloc.stop()
        report.duration_s = time.monotonic() - start
    return report


def repro_command(seed: int, case_id: str, mode: str, plant: str | None) -> str:
    return (f"python -m brain chaos {'--full ' if mode == 'full' else ''}--seed {seed} --only {case_id}"
            + (f" --plant {plant}" if plant else ""))


def format_failure(r: CaseResult) -> str:
    head = f"{r.status.upper()} {r.case_id} ({r.name}) [{','.join(r.tags) or 'valid'}] case seed {r.seed}"
    lines = [head] + [f"    {inv}: {msg}" for inv, msg in r.violations[:5]]
    if r.status == "xpass":
        lines.append(f"    recorded defect now passes: {r.known_defect}. Remove known_defect from the case.")
    lines.append(f"    reproduce: {r.repro}")
    return "\n".join(lines)


def write_results(report: RunReport, directory: Path, canaries: dict[str, str] | None = None) -> Path:
    """Write the results file (refusing if a canary secret would end up in it) and prune old ones."""
    text = json.dumps(report.to_json(), indent=1, sort_keys=True, default=str)
    canaries = report.canaries if canaries is None else canaries
    if canaries and monitors.scan_secrets(text, canaries):
        raise HarnessError("refusing to write results: a canary secret is in them")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}-{report.commit}-{report.mode}.json"
    path.write_text(text, encoding="utf-8")
    old = sorted(directory.glob("*.json"))
    for stale in old[:-T.RESULTS_KEEP]:
        stale.unlink()
    return path


def summary(report: RunReport) -> str:
    c = report.counts()
    lines = [f"chaos {report.mode}: seeds {report.seeds}, {len(report.results)} cases in {report.duration_s:.1f} s "
             f"(commit {report.commit}{', PLANTED BUG ' + report.plant if report.plant else ''})",
             "  " + ", ".join(f"{k} {v}" for k, v in sorted(c.items()))]
    for problem in report.static + report.harness_problems:
        lines.append(f"  STATIC {problem}")
    lines.append("  invariants:")
    for inv, v in report.invariants().items():
        extra = f" ({v['expected_violations_of_recorded_defects']} expected, recorded defects)" \
            if v["expected_violations_of_recorded_defects"] else ""
        lines.append(f"    {inv:26} {v['verdict']}{'' if v['verdict'] == 'held' else ' x' + str(v['violations'])}{extra}")
    xf = [r for r in report.results if r.status == "xfail"]
    lines.append(f"  static recorded defects: {sum(lints.BASELINE.values())} asserts used for checks "
                 f"({lints.BASELINE_FIX}, E2 baseline)")
    if xf:
        lines.append("  recorded defects reproduced (expected failures):")
        for d in sorted({r.known_defect or "" for r in xf}):
            lines.append(f"    {d} ({sum(1 for r in xf if r.known_defect == d)} cases)")
    lines.append(f"  entry points with no experiment yet: {len(report.uncovered)} (stage 1b adds them)")
    if report.mode == "quick" and report.duration_s > T.QUICK_SUITE_MAX_S:
        lines.append(f"  WARNING quick mode took {report.duration_s:.1f} s, over its {T.QUICK_SUITE_MAX_S:g} s budget")
    lines.append("  RESULT: " + ("PASS" if report.ok else "FAIL"))
    return "\n".join(lines)


# ---- command line ----------------------------------------------------------------------------------------


def _seed(text: str) -> int:
    try:
        value = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"seed must be a whole number, not {text!r}") from None
    if not 0 <= value <= T.SEED_MAX:
        raise argparse.ArgumentTypeError(f"seed must be 0 to 2^63-1, not {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m brain chaos",
                                 description="Chaos and boundary test harness (card X-01). See tools/chaos/README.md.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_const", const="quick", dest="mode",
                      help="deterministic, seconds (default; runs with pytest)")
    mode.add_argument("--full", action="store_const", const="full", dest="mode",
                      help="more seeds and longer sequences (gates, major features)")
    ap.add_argument("--seed", type=_seed, action="append", help="run with this seed instead (repeatable)")
    ap.add_argument("--only", action="append", default=[], metavar="CASE_OR_EXPERIMENT",
                    help="run only matching case ids (exact, prefix, or glob), as printed in a failure")
    ap.add_argument("--check-manifest", action="store_true", help="only the manifest drift check")
    ap.add_argument("--manifest-stub", action="store_true",
                    help="print manifest lines for entry points the manifest lacks, then exit")
    ap.add_argument("--list", action="store_true", help="list experiments and their case counts, then exit")
    ap.add_argument("--scan-logs", nargs="?", const=str(REPO_ROOT / "data" / "logs"), metavar="DIR",
                    help="log hygiene of existing text logs (default data/logs): counts and line numbers only")
    ap.add_argument("--plant", metavar="BUG", help="harness self-test: run with a known bug reintroduced")
    ap.add_argument("--strict-coverage", action="store_true", help="fail when a manifest entry has no experiment")
    ap.add_argument("--results-dir", type=Path, default=RESULTS_DIR, help="default data/chaos")
    ap.add_argument("--no-results", action="store_true", help="do not write a results file")
    ap.add_argument("--allow-real-data", action="store_true",
                    help="let cases touch the real data folder, .env and token store (no experiment needs it yet)")
    ap.add_argument("--allow-real-phone", action="store_true",
                    help="let cases start adb (no experiment needs it yet)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = args.mode or "quick"
    try:
        if args.check_manifest or args.manifest_stub:
            preflight()
            m = manifest_mod.load_manifest()
            found = manifest_mod.discover()
            d = manifest_mod.drift(m, found)
            if args.manifest_stub:
                print(manifest_mod.stub(d.missing_in_manifest, found) or "# the manifest lists every entry point")
                return 0
            for line in d.lines():
                print(line)
            print(f"manifest: {len(m.entries)} entries, {len(m.gates)} gates; drift: {'none' if d.ok else len(d.lines())}")
            return 0 if d.ok else 1
        if args.scan_logs:
            from . import log_hygiene

            n, findings = log_hygiene.scan_dir(Path(args.scan_logs))
            for f in findings[:200]:
                print(f"  {f}")
            print(f"log hygiene: {n} files, {len(findings)} findings {log_hygiene.summarise(findings)}")
            return 0 if not findings else 1
        if args.list:
            for exp in load_cases().values():
                for seed in args.seed or (T.QUICK_SEEDS if mode == "quick" else T.FULL_SEEDS):
                    cases = generate(exp, seed, mode)
                    by = Counter(c.cls.value for _, c, _ in cases)
                    print(f"{exp.id:40} seed {seed}: {dict(by)}  [{exp.blast_radius.value}] {exp.hypothesis[:60]}")
            return 0
        if args.allow_real_data or args.allow_real_phone:
            print("WARNING: the blast radius is widened (--allow-real-data / --allow-real-phone)")
        report = run(mode, args.seed, args.only, args.plant, allow_real_data=args.allow_real_data,
                     allow_real_phone=args.allow_real_phone, strict_coverage=args.strict_coverage,
                     progress=print)
    except (HarnessError, manifest_mod.ManifestError) as e:
        print(f"chaos: cannot run: {e}", file=sys.stderr)
        return 2
    print(summary(report))
    code = 0 if report.ok else 1
    if not args.no_results:
        try:
            path = write_results(report, args.results_dir)
            print(f"  results: {path}")
        except (OSError, HarnessError) as e:
            print(f"  results NOT written: {e}")
            code = 1
    return code
