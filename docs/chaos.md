# Chaos and boundary testing: how to use it

Card X-01 step 17. The harness lives in `tools/chaos/` (details: `tools/chaos/README.md`). This page is the short
practical version.

## What it proves

Every entry point (a tool, a protocol message or field, a CLI flag, a config key, an environment variable, a file the
code reads, a Body method, an Android component) is fed valid, invalid, hostile and failing input. Seven invariants must
hold; the first line of each row says what proves it.

| Invariant | What proves it |
| --- | --- |
| no_crash | the case raises nothing; no thread dies; no traceback is logged (a deliberate fault sets `isolated_fault`); a child process prints no traceback |
| no_forbidden_side_effect | an audit hook refuses and reports writes outside the sandbox, the real `data/`, `.env` or token store, process starts nobody allowed, sockets to anything but this process's loopback ports |
| validate_before_use | each experiment's oracle (accept exactly the valid input, refuse the rest with a structured error) |
| bounded_resources | a per-case timeout, log-line and log-size limits, queue depths, leaked tasks; the CLI tests time every child |
| no_secret_in_output | canary secrets (token, API keys) are looked for in logs, errors, outputs, results and child output |
| right_to_refuse, fail_closed | each experiment's oracle: a refusal is clear, short, and nothing falls back to a weaker mode |

A deliberate fail-closed refusal with a clear message is a pass. A traceback, hang, runaway resource, forbidden side
effect or secret in output is a failure.

## Run it

    python -m brain chaos                      # quick, deterministic, about 30 s (also runs inside pytest)
    python -m brain chaos --full               # 5 seeds, every boundary, memory traced
    python -m brain chaos --check-manifest     # drift between the code's entry points and the manifest
    python -m brain chaos --list               # experiments and case counts
    .\.venv\Scripts\python -m pytest -q        # the whole suite, including the quick run and the tests below
    $env:HYPOTHESIS_PROFILE = "chaos-full"; .\.venv\Scripts\python -m pytest tests\test_stateful_properties.py

The summary ends with `entry points with no experiment yet: N`. The goal is 0; entries the Python harness cannot
exercise are `[[exempt]]` records in the manifest (a reason and the card that will cover them) and are counted
separately. A stale exemption fails the run.

## The layers

| Layer | Where | Notes |
| --- | --- | --- |
| Experiments (seeded, one per area) | `tools/chaos/cases/*.py` | the boundary matrix gives min, max, one over, zero, empty, null, wrong type, wrong encoding and oversize values for free |
| CLI end to end | `tests/test_cli_end_to_end.py` | every command in a child process with hostile argv; exit 0, 1 or 2 only, no traceback, bounded time, nothing outside the sandbox. A NUL character or non-UTF-16 text cannot be put in a Windows command line at all |
| Stateful properties (Hypothesis) | `tests/test_stateful_properties.py` | Body lifecycle (SimBody, LinkBody), the handshake state machine against an unauthenticated peer, random tool-registry calls with an audit hook. Quick mode is derandomised (`tests/conftest.py` profiles) |
| Shared hostile corpus | `protocol/corpus/v0-hostile.json`, made by `tools/gen_hostile_corpus.py` (`--check` in CI) | the Python validator decides accept or reject; `HostileCorpusTest.kt` asserts the phone's parsers agree and never throw. Every disagreement is a named, strict expected failure in that test |
| Lints | `tools/lint.py`, `tests/lint_baseline.toml`, `tests/test_lint.py` | E1a and E1b, below |
| Traceability | `tools/trace.py check`, `tests/trace_map.toml` | maps tests to T- and R- ids |

Regenerate the corpus after a protocol change: `.\.venv\Scripts\python tools\gen_hostile_corpus.py`, then run
`.\gradlew.bat testDebugUnitTest --offline` in `body-android` (with `JAVA_HOME` set to Android Studio's `jbr`) and
`.\gradlew.bat --stop` afterwards.

## Add an entry point and its cases

1. Build the feature. `python -m brain chaos --check-manifest` lists the new entry points.
2. `python -m brain chaos --manifest-stub`, paste the lines into `tools/chaos/manifest.toml` (`entry = [...]`), review
   `card` and `boundary`. A new gate function goes in `[[gate]]` with the limits it enforces.
3. Add `tools/chaos/cases/<area>.py` with `@experiment(id=..., card=..., entries=(...), hypothesis=..., blast_radius=...,
   valid=..., invalid=..., hostile=..., failure=...)`. Give every class, or `not_applicable("why")`. Use `matrix_cases`
   for one input's boundary set. Generators use `g.rng` (never `random` or the clock) and `g.mode` to keep heavy cases
   (megabyte frames, processes) for `--full`; quick mode must stay under 30 s.
4. If the entry point really cannot be exercised yet (a phone, a later card), add an `[[exempt]]` record instead
   (`ids`, `card`, `reason`), never leave it silently uncovered.
5. A gate function needs edge tests: `@pytest.mark.gate("<dotted name>", limit="<NAME>", edge="below"|"at"|"above")` for
   each limit it lists, and one `edge="wrong_type"` test (see `tests/test_gate_edges.py`).
6. `python -m brain chaos`, `pytest`, `mypy` (the `[tool.mypy] files` list in `pyproject.toml`) and `python tools/trace.py check`.

## Read a failure and replay it

    FAIL P0-06.sim.media/invalid/12 (read_wav_empty) [N4] case seed 372398982657866761
        no_crash: EOFError:  [EOFError]
        reproduce: python -m brain chaos --seed 1 --only P0-06.sim.media/invalid/12

The id is `<experiment>/<class>/<index>`; the same seed always makes the same cases, so the reproduce line replays
exactly that case (use the whole experiment id with `--only`, not a prefix of it). A Hypothesis failure prints a
minimal example and a `@reproduce_failure(...)` line; the profiles are derandomised, so a failing example is stable.
`XFAIL` is a recorded defect (`known_defect="X01B-nn: ..."`, listed in `tests/chaos/test_chaos_known_defects.py`);
`XPASS` means it is fixed: remove the marker and move the entry to FIXED in the same commit.

## What the lints do not cover

E1a (`tools/lint.py e1a`) is the conservative subset of docs/testing/00 section 4.4: in public functions and methods of
the boundary modules it flags a parameter annotated `Any`, `object`, `dict`, `Mapping` or raw bytes (also inside
unions and containers). Not covered: missing annotations; bare `str` parameters with names such as `text` or `path`;
`NewType` forgery outside its gate module; `@trusted_input` markers (the decorator does not exist yet); methods that
override a base class (skipped); private functions; Kotlin. Existing violations are in `tests/lint_baseline.toml`
keyed by symbol; a new one fails, a fixed one makes its entry stale (and fails until deleted), counts and
`meta.max_entries` only fall.

E1b (`tools/lint.py e1b`, and `tests/test_lint.py` in a whole-suite run) needs, for each `[[gate]]` and each limit it
lists, below, at and above tests plus one wrong-type test. Gates still missing some are baselined too.

## Known limits of the harness

Effects outside Python's audit points (C extensions, `ctypes`, what a child process does) are not seen; the children in
`tests/test_cli_end_to_end.py` are judged by their output, their exit code and the files they leave. Another session
using the real `data/` folder at the same time can make a before and after comparison lie, so a child's files are
recognised by what they mention (the sandbox path, the test server's port).
