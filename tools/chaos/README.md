# Chaos and boundary test harness (card X-01, stage 1a)

The harness proves two things about every entry point the system exposes: it does what it should for
valid input, and for invalid, hostile or unlucky input it never crashes, never leaves a traceback,
never hangs, never runs away with resources, never does anything it must not, and never shows a secret.

## Run it

    python -m brain chaos                    # quick: deterministic, about 15 s (also runs inside pytest)
    python -m brain chaos --full             # gates and major features: 5 seeds, every boundary, memory traced
    python -m brain chaos --seed 7           # another seed (0 to 2^63-1)
    python -m brain chaos --only P0-06.registry.call/invalid/12    # one case, as printed in a failure
    python -m brain chaos --list             # experiments and their case counts
    python -m brain chaos --check-manifest   # drift check only
    python -m brain chaos --manifest-stub    # manifest lines for entry points the manifest lacks
    python -m brain chaos --plant recursion  # harness self-test: a known bug reintroduced in memory
    python -m brain chaos --scan-logs        # log hygiene of data/logs (counts and line numbers only)

Exit code 0: every invariant held (expected failures of recorded defects allowed). 1: something failed,
including a recorded defect that now passes. 2: the harness could not run (bad arguments, unreadable
manifest, unsafe environment). Results go to `data/chaos/<date>-<commit>-<mode>.json` (git-ignored,
last 50 kept); `--no-results` skips the file.

Inside pytest: `tests/chaos/` runs the quick suite once per session, the planted-bug proofs, the monitor
proofs and the recorded-defect list. `pytest --run-slow` adds the full run.

## Read a failure

    FAIL P0-05.server.session/hostile/0 (forged_log_hello) [N7] case seed 1234...
        validate_before_use: a hello whose software holds a control character was answered with 'hello'
        reproduce: python -m brain chaos --seed 1 --only P0-05.server.session/hostile/0

The case id is `<experiment>/<class>/<index>`; the same seed always generates the same cases, so the
reproduce line replays exactly that case. `XFAIL` cases are recorded defects (their `known_defect`
names the H-item or finding); `XPASS` means a recorded defect is fixed: remove its `known_defect`
marker and its entry in `tests/chaos/test_chaos_known_defects.py` in the same commit.

## The seven invariants and the monitors that check them

| Invariant | Checked by |
| --- | --- |
| no_crash | an exception from the case; an exception an event loop could not deliver; a dying thread; an unraisable exception; `SystemExit`; ANY logged traceback (unless the case injected the fault itself: `isolated_fault=True`); an isolation point's "failed handling" record |
| no_forbidden_side_effect | audit hook plus `__import__` and proactor-connect wrappers: writes outside the sandbox, any touch of real `data/`, `.env` or the token store, secrets read outside `brain/transport/auth.py`, process starts the experiment did not allow, sockets to anything but this process's loopback ports, binds to non-loopback, forbidden imports from project code. Forbidden effects are refused as well as reported |
| validate_before_use | each experiment's oracle; control characters in a log line (log forging) |
| bounded_resources | case timeout (10 s quick, 60 s full), Python heap peak (full mode), log record size `LOG_RECORD_MAX_BYTES`, log lines per case `PEER_LOG_LINES_PER_MIN_MAX`, queue depth (`ctx.monitors.observe_queue`), leaked tasks |
| no_secret_in_output | canary secrets (token, API keys) and secret shapes in log records, tracebacks, case outputs, errors and the results file |
| right_to_refuse, fail_closed | each experiment's oracle |

Structured events (card X-02, `docs/observability.md`): every case runs with
`tools.chaos.monitors.structured_sink` registered on the brain's event log (`brain.obs.LOG`), so every
event a case causes is checked by the same hygiene rules (`tools/chaos/log_hygiene.py`: secrets, control
characters, size), counted against the case's line budget, and an event of a crash type
(`supervisor.crashed`, `tool.failed`, `process.crash`: an isolation point caught an unexpected exception) is
a `no_crash` violation unless the case injected that fault (`isolated_fault=True`). Each case starts with
full per-type event budgets (`EventLog.reset_limits`), so its event count does not depend on the case before.

X-02's own experiments (`tools/chaos/cases/observability.py`): `X-02.obs.emit` (hostile fields, canary
secrets, forged lines, a failing event file and sink), `X-02.obs.state` (corrupt state files),
`X-02.obs.viewer` (damaged and hostile event and health files), `X-02.obs.supervisor` (crashing, fatal and
stubborn components), `X-02.obs.demo` (the scripted session with the link, camera and microphone
unavailable). Manifest kinds added: `event_type` (checked attribute `status`: live or reserved) and the stores
`health.json`, `state.json`, `events.jsonl`, `crash_report`. The process launcher starts processes, which the
sandbox refuses: it has an `[[exempt]]` record and is covered by `tests/test_obs_crash_barrier.py`.

Known blind spots: effects outside Python's audit points (C extensions with their own I/O, `ctypes`
calls, what a child process does); a thread abandoned by a timed-out case is attributed to the next
case; quick mode does not trace memory (it doubles the run time); `os._exit` in a case ends the run.

## Add a card's entry points and cases (the definition-of-done item)

1. Build the feature. Run `python -m brain chaos --check-manifest`: it lists every new entry point the
   code now has (tools, messages and fields, commands, Body methods, CLI flags, config keys, env vars,
   file access sites, network or process imports, Android components and actions).
2. `python -m brain chaos --manifest-stub`, paste the lines into `entry = [...]` in
   `tools/chaos/manifest.toml`, and review `card` and `boundary` (B-ids, docs/testing/00 section 3).
   New gate functions go in the `[[gate]]` table with the Appendix A limits they enforce.
3. Add `tools/chaos/cases/<area>.py`:

        from tools.chaos.api import experiment, failure, hostile, invalid, not_applicable, valid
        from tools.chaos.matrix import Spec, matrix_cases

        def gen_valid(g):            # g.rng is seeded: never use random or the clock directly
            yield valid("plain", {"text": "hello"})

        def gen_invalid(g):          # N1 to N6; the matrix gives the standard boundary set for free
            yield from (c for c in matrix_cases("text", Spec("string", min_length=1, max_length=2000),
                                                make=lambda b: {"text": b.value, "accept": b.accept})
                        if c.cls.value == "invalid")

        def gen_hostile(g):          # N7 injection, N8 order, N9 replay, N16 authority
            yield hostile("path_text", {"text": "..\\..\\.env"}, "N7")

        @experiment(id="P2-05.tts.speak", card="P2-05", entries=("tool.speak", "field.speak_local.text"),
                    hypothesis="speak refuses text over SPOKEN_TEXT_MAX_CHARS and never raises",
                    blast_radius="sim", valid=gen_valid, invalid=gen_invalid, hostile=gen_hostile,
                    failure=not_applicable("covered by P0-06.registry.call"))
        async def check(case, ctx):
            result = ...                       # drive the entry point with case.data
            ctx.expect(result.ok == (case.cls.value == "valid"), "validate_before_use", "wrong verdict")
            return result                      # scanned for secrets, kept (shortened) in the report

   Every class must be given; `not_applicable("why")` states why one does not apply. Tags must belong
   to their class, case names must be unique, and an experiment must name real manifest entries: the
   harness refuses anything else at load time. A case the code fails today because of a recorded
   defect gets `known_defect="H-nn: ..."` (strict: it becomes a failure once the defect is fixed).
4. `python -m brain chaos` and `pytest` must pass. `python tools/trace.py check` refuses to call a card
   done on planned-only tests.

## Install the pre-push hook (optional; nothing installs it for you)

    copy tools\chaos\pre-push.sample .git\hooks\pre-push

It runs the drift check and the quick suite before a push and refuses the push if either fails.

## Files

`api.py` plug-in API · `matrix.py` boundary-value matrix · `manifest.py` + `manifest.toml` entry points
and drift check · `runner.py` runner and CLI · `monitors.py` invariant monitors · `log_hygiene.py` log
rules · `sandbox.py` blast radius · `fuzz.py` seeded generators · `planted.py` planted bugs ·
`lints.py` E2 assert lint · `thresholds.py` harness thresholds (Appendix A.9) · `pytest_plugin.py`
fixtures (`chaos_monitors`, `chaos_sandbox`, `chaos_quick_report`) · `cases/` experiments.
Traceability: `tools/trace.py` with `tests/trace_map.toml`.
