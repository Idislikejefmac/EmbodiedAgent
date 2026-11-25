# Where the project stands

The current state. [`../CLAUDE.md`](../CLAUDE.md) is the entry point and loads this file. Last updated 25 Nov 2026.
Update it whenever a session ends with the state changed.

## Now

- **At gate G1, a hard stop.** All of Phase 0 (P0-01 to P0-07) is built. No new feature card starts until Jeff runs the
  G1 hands-on test and chooses GO, ADJUST or NO-GO. The record and checklist: [`gates/G1.md`](gates/G1.md) (a draft).
- **The 24 Nov work below is committed and pushed** to `main` (the commit after `0520566`). Commit and push only when
  Jeff asks.
- **The full chaos run passed on `7fbd7ca`** (25 Nov: seeds 1 to 5, 23,615 of 23,615 pass, all invariants held, about
  11 minutes), recorded in `gates/G1.md`. It stays valid while only documents change. Run
  `python -m brain chaos --full` when Jeff asks, with no Android build running (low memory stopped an earlier run).
- **The phone has an older app build.** The Android sources changed on 24 Nov; install the new debug APK before G1.

## Next, in order

1. Jeff: G1 hands-on test (`gates/G1.md`). It needs decisions on paid calls (`[models] allow_live_calls`, false now)
   and DEC-109 (sign test with a simulated picture at G1, camera at G2; recommended).
2. Before P1-01: decisions DEC-054 (image policies), DEC-016 (no separate image decoding process for v1), DEC-040 (who
   may turn quiet mode off; unlocks H-17). All three have recommendations in [`testing/DECISIONS.md`](testing/DECISIONS.md);
   **none is answered**. Also the media gate (docs/testing/50 section 4.D) is not built as far as a search shows.
3. Then P1-01 to P1-05 (gate G2), as in the plan's implementation order.

## Built

| Card or area | State | Doc |
| --- | --- | --- |
| G0 phone viability | GO with adjustments; anchor drift not measured | [`gates/G0.md`](gates/G0.md) |
| P0-01 to P0-04 | Built | the plan, [`migration.md`](migration.md) |
| P0-05 link protocol | Built; re-checked on the phone 15 Nov. Clock-stability definition awaits Jeff | [`gates/P0-05-phone-recheck.md`](gates/P0-05-phone-recheck.md) |
| P0-06 body interface, tools, egress guard | Built | [`body_tools_egress_v0.md`](body_tools_egress_v0.md), [`adding_a_body.md`](adding_a_body.md) |
| P0-07 agent loop, phone speech, Poke | Built; live model calls succeeded on 15 Nov | [`agent_loop.md`](agent_loop.md) |
| X-01 chaos harness | Stages 1a and 1b; `tests/trace_map.toml` says in progress | [`chaos.md`](chaos.md) |
| X-02 live trace and crash barrier | Built | [`observability.md`](observability.md) |
| Profiles | Built | [`profiles.md`](profiles.md) |
| P2-04 speech to text (local half) | Built ahead of order | [`stt.md`](stt.md) |

## 24 Nov 2026 session

- **Seven chaos defects fixed** (X01B-01, 03 to 08): WAV reading and writing validate and raise ValueError; deeply
  nested data files raise ValueError; `check_gates` reports odd gate names; `LinkBody(status_timeout_s=)` is checked;
  chaos `--seed` takes ASCII digits only; `icacls.exe` is found through Windows, not the `SystemRoot` variable. Their
  cases are now permanent regression cases (`tests/chaos/test_chaos_known_defects.py`).
- **H-16 done:** `PersonName`, `SpeakerStatus`, evidence, score range -1 to 1, bounded names in the tool catalogue. A
  `Speaker` that gives no status is `unavailable`.
- **H-02 partly:** the phone no longer throws on a frame without `type`, and checks binary id and timestamp ranges.
- **H-14 partly:** Android lint (`lintDebug`, `lintRelease`) passes after declaring the camera feature optional.
- **H-03 partly:** `requirements.lock` and `tools/make_lockfile.py`, checked by `tests/test_lockfile.py`.

Last verification, same day: pytest 2,673 passed, 7 skipped, 24 deselected, 2 expected failures; mypy clean; hostile
corpus up to date; quick chaos 2,479 of 2,479; Android unit tests, both lints and the debug build passed. Pytest and
mypy ran after the last code change.

## Open items

| Item | Detail |
| --- | --- |
| Phone and Python disagree (H-02) | 61 listed corpus cases in `HostileCorpusTest.kt`: org.json accepts malformed JSON (16), duplicate keys (4), payloads not checked against the schema (39), two stricter cases. Plus one token-trimming case |
| Android checks (H-14) | No detekt; no release-absence or manifest checklist (`tools/android_checklist.py` does not exist) |
| Lockfile (H-03) | An install from it has never been completed (a trial was stopped for downloading too slowly). Hashes came from this laptop's configured package feed. `pip-audit` waits on DEC-101; no `docs/licences.md` |
| Other hardening | H-15 (phone token in the Keystore) before P2-02; H-17 on DEC-040; H-35 before P4-08. List and older status: docs/testing/50 section 4.A |
| Traceability | `tools/trace.py check` fails with 18 or 19 problems: risks without tests, and 9 mismatches between trace markers and docstrings in the P0-07 tests. Commit `0520566` shows the same 18. Run `tools/trace.py collect` first: `check` reuses `data/trace/collected.json` even when it is stale. The count varies because the owner of R-SYS-16 is a tie broken by hash order |
| Quick chaos is slow | About 42 s against its 30 s budget on 24 Nov; cause not investigated |
| Outdated text in code | `brain/obs/health.py` DEFAULTS says there is no speech output yet; the `brain/obs/state.py` docstring calls the spend counters placeholders. Fix with the next code change |
| Real-vendor pytest | No record that `tests/test_real_vendor.py --run-real-vendor` has been run |
| Step 0 scope | docs/testing/50 section 2.1 asks more of a gate than the plan; proposal in `gates/G1.md`, not agreed |
| Company laptop | It will be returned: data must be moved off and securely deleted first (DEC-020, DEC-021 open). The migration tool leaves out `profiles/` (see [`migration.md`](migration.md)) |

## How to work here

- `.\.venv\Scripts\python -m pytest -q` (about 7 minutes), `-m mypy`, `-m brain chaos` (quick), `-m brain chaos --full`.
- Android, from `body-android` with `JAVA_HOME` set to Android Studio's `jbr`:
  `.\gradlew.bat testDebugUnitTest lintDebug lintRelease assembleDebug --offline`, then `.\gradlew.bat --stop`. The
  Kotlin compiler process stays running after `--stop` (about 2 GB); on this 16 GB laptop end it before a full chaos run,
  checking first that it is not another project's (`PersonalAssistant` also builds here).
- The documents in `docs/testing/` (apart from short later status notes) are the 13 Nov test design, written against commit `bc7610c`. Their "exists" and
  "planned" statements are older than the code: verify in the source before relying on them.
