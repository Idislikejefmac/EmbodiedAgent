# Independent Review of the Test and Risk Design (docs 00, 10, 20, 30, 40, 45)

Reviewer: Claude (Opus 5.5), 13 Nov 2026. Read-only review. Doc 50 was excluded as instructed. Evidence comes from the documents, `EmbodiedAgent-Plan-and-Implementation-Order.md`, the code at commit `bc7610c` plus the untracked `docs/testing/`, `pytest --collect-only` (361 items) and small venv-Python calculations.

Method. I read doc 00 and the plan's X-01, tool, gate and command-boundary sections myself. Each feature document was then reviewed in depth by a separate read-only helper pass. I re-checked every blocker and most must-fix items myself against the files; where I did, it is marked **(verified)**. Line numbers are document lines.

## A. Verdict

**(a) Does the set meet the brief?**

**Partly. It is not yet buildable as it stands.**

What it does well:
- Breadth is excellent:
  - every card P0-01..R-09 and X-01 has a section;
  - the five promises, B1-B16 and N1-N16 are applied card by card;
  - all 260 risk rows have Score = L x I (checked by script);
  - the documents are unusually candid about what is Planned versus Existing.

Where it falls short of the brief:
- **"Never operate on unverified input" and "every function self-checks"** rest mostly on doc 00's E1 lint, a `require`/`ensure` helper, a `limits.py` and a typed error hierarchy. None of these exist, E1 cannot work as specified, and existing code already breaks E2 and §3.1.
- **Named limits:** many test oracles still read "N", "decide", "a stated size" or "stated rule". Many limits are literals, which doc 00 §3.1 calls a defect.
- **Defects that would mislead a builder:**
  - a safety test oracle has the wrong sign;
  - two physically unsafe test procedures;
  - a circular statistical method for the top identity risk;
  - a false claim that the test suite cannot reach the paid API.
- **Gates:** G0-G10 are not designed as gates; only G11 is.

**(b) Top ten corrections:** see section B.

**(c) Confidence in claims about existing code: high.**
- 52 claims in doc 10 were checked; 40 are confirmed and 12 are wrong or partly wrong. The wrong ones are mostly about names and counts, but 3 overstate existing coverage.
- In docs 20/30/40/45, about 60 further claims were checked; about 6 are wrong.
- All test counts are exact: 361 Python and 25 Kotlin tests.
- 15 of the asserted code defects were confirmed directly. One was already fixed in code (ArCheck). One code defect that no document asserted turned up (heading -pi, M10).

## B. Top ten corrections before anyone builds from this

1. **Fix the inverted bearing oracle** in doc 40 T-P4-05-004 (B1).
2. **Rewrite the three unsafe robot procedures** and gate R-03 floor work behind the R-02 ladder (B2).
3. **Replace the circular threshold and bound method** in doc 30 with disjoint session-level partitions. Use a dependence-aware n, and IAPMR instead of APCER (B3, M5).
4. **Make the test suite deny-all for egress** (`tests/conftest.py` currently allows `api.anthropic.com`), and correct doc 20 §2.4 (B4).
5. **Redesign E1** as a "no raw types past the gate" lint, and add an adoption baseline so existing code can comply incrementally (M1).
6. **Repair the ID scheme:**
   - unique risk IDs (R-FND-26 is two different risks);
   - a risk prefix that cannot collide with robot cards R-01..R-09;
   - one canonical score per cross-document risk;
   - test IDs for gates and X-01 (M2).
7. **Write gate designs for G0-G10** (step 0, each step mapped to test IDs, record template, GO/ADJUST/NO-GO rule), and flag the six plan gate defects listed in M3.
8. **Replace every undecided oracle** ("N", "decide", "stated rule", "zero or bounded") with a named constant or a blocking decision ID (M6).
9. **Correct the 12 wrong claims about existing code in doc 10,** especially the three that overstate coverage (M7).
10. **Add a feasibility section to doc 00:**
    - Windows timer granularity;
    - missing libraries;
    - mutation tool on Windows;
    - ROS/Nav2 needs Linux;
    - consent before recording family voices;
    - company-laptop data exposure (M8, M9).

## C. Findings

### Blockers

**B1. Doc 40 §5, T-P4-05-004 (line 522): the safety oracle has the wrong sign. (verified)**
- Evidence:
  - The test asserts `sign(bearing) == -sign(dot(t - p, R*(1,0,0)))`.
  - T-P4-05-001 (line 519) requires a target at (+3,0,0), seen from heading 0, to give +90 (right). There dot = +3, so the oracle demands -1.
  - A correct engine fails this test. An engine "fixed" to pass it mirrors every instruction, which is R-SPA-26, the G9 NO-GO case.
  - Line 59 also uses `t` where it should use `t - p`.
- Correction:
  - Change the oracle to `+sign(dot(t - p, R*x))`.
  - Add three hand-computed golden cases that pin the oracle itself.
  - Consider making the bearing CCW-positive, so no negation exists anywhere.

**B2. Doc 45 §10: unsafe or contradictory physical test procedures. (verified)**
- **T-R-02-060 (line 803): "Stall: hold the wheel by hand at low torque".** This puts a hand on a live drive wheel, which is the pinch hazard R-ROB-14 itself.
  - Correction: use a brake fixture or clamped load, with the e-stop held by a second person.
- **T-R-02-031 (line 774): "A mannequin or a person walks into the path at cap speed".** It is listed as an S3 exit test, but S3 is defined as "no people or pets" (line 883).
  - Correction: surrogates only until after G11.
- **R-03 floor drives.** T-R-03-004/-005 ("drive forward 1 m") run on the floor. Plan R-03 depends only on R-01, and the supervised protocol binds only "stages from S2" (line 886).
  - Correction: R-03 floor steps require the R-02 S1 exit and the supervised protocol.
- **T-R-02-056** deep-discharges a loaded lithium battery.
  - Correction: use the bench supply only.

**B3. Doc 30 §2.2 and §11: thresholds are chosen and "bounded" on the same data. (verified)**
- Evidence:
  - T-P3-09-005 (line 936) sets "Accept threshold = highest impostor score in the threshold-choice set plus a recorded margin … the zero-FA upper bound 3/n stated for every pair."
  - Zero false accepts on that set is true by construction, so the 3/n bound is meaningless. Line 101 says the bound is "on the held-out trial set", but P3-09 uses that set to choose the threshold.
  - The plan has the same flaw (step 5, "false accepts on the test data are zero"), and doc 30 does not flag it.
- Correction:
  - Use session-level disjoint partitions: enrol / choose threshold / evaluate. Use leave-one-session-out if data is short.
  - Add a test that refuses to print a bound computed on the choice partition.
  - Flag the plan text.

**B4. Doc 20 §2.4 (line 67): "any accidental real call fails with `EGRESS DENIED`" is false. (verified)**
- Evidence:
  - `tests/conftest.py` installs `EgressPolicy.from_config(load_config())`.
  - `config.toml [egress] allowed_hosts = ["api.anthropic.com"]`.
  - So under the suite a real Anthropic call goes through once a key is in `.env`. The vendor hosts will follow when P2-04 adds them.
  - Every "no API cost" and "no audio leaves the house" test in docs 10/20/30/40 relies on this guard.
- Correction:
  - conftest installs a loopback-only policy.
  - `@real_vendor` tests run in a separate pytest process with their own policy (the guard cannot be widened once installed, `egress.py:16-17`).
  - Add a test that `api.anthropic.com` is denied in the normal suite.

### Must fix

**M1. Doc 00 §3.1, §4, §4.3: the enforcement layer does not exist, cannot work as written, and has no adoption path.**
- **Missing pieces:**
  - `brain/limits.py`, `require`/`ensure`, `@trusted_input` and the S5 error hierarchy do not exist. The only `require` is `Body.require(capability)` at `base.py:199`.
  - `tests/unit/`, `tests/contract/` and the other homes in §5 do not exist; tests are flat in `tests/`.
- **Existing code already fails doc 00 (verified):**
  - E2: `assert` at `protocol.py:121` and `tools.py:407,412,415,419,520,559`.
  - E4: broad `except Exception` at `tools.py:326,509,535,555,600`, `egress.py:328,347` and `server.py:207,300`.
  - §3.1: limits are spread over 9 modules.
  - Doc 00 gives no baseline or burn-down, so in practice either every gate fails or the standard is ignored.
- **E1 as specified cannot work.** It says "each public function must call `require`/a validator, accept only gate-produced types, or carry `@trusted_input`".
  - "Calls a validator" is satisfied by validating one argument out of five, or by a call in a dead branch. That is a false pass.
  - Every Body override, property, `__post_init__`, async generator, trivial getter and callback (`on_message`) would need a marker. That is mass false failure, and the result is marker spam that means nothing.
  - "Accepts only gate-produced types" needs a registry of gate types and resolution of `Optional`, unions and generics. That is mypy's job, not an AST scan's.
- **Correction:**
  - Replace E1 with two mechanical checks.
    - E1a: in non-gate boundary modules, no public signature takes `dict`, `Any`, `object`, `bytes` or bare `str` for external data. Use frozen dataclasses or `NewType`s produced by named gate functions; mypy strict enforces the rest.
    - E1b: the X-01 entry-point manifest lists each gate function, and each gate has negative-class tests at below / at / above every limit it uses.
  - Keep `@trusted_input` only for the few functions that genuinely take raw input from a gate.
  - Add an "Adoption" section: a checked-in baseline file of existing violations. The checks fail only on new violations, and the baseline must shrink by card.
  - Narrow S12/E9 docstring contracts to gate functions and side-effecting functions.

**M2. IDs and scores are not stable or unique (doc 00 §7, §8).**
- **Risk-ID clashes (verified by grep):**
  - R-FND-26 names two different risks (doc 10 lines 396 and 671).
  - Doc 30 restates R-ID-19 and R-ID-29 with different text.
  - Doc 20 has R-VOX-13 three times (616, 882, 971) and R-VOX-10/-19 twice each, with different wording and different tests.
  - R-FND-18 and R-FND-50 are the same risk.
  - R-FND-31a is non-standard.
- **Card IDs R-01..R-09 match the risk pattern.** Doc 45 tables start rows with `| R-01 |`, and `tools/trace.py` will mis-parse them.
- **Test IDs outside `T-<card>-<nnn>`:**
  - `T-X01` (should be `T-X-01`), `T-AND` and `T-G11` are outside the scheme.
  - T-P0-04-013 is used for two tests.
  - Doc 45 stages S0-S4 and layers L0-L5 collide with rules S1-S12 and with L for likelihood.
- **The same risk is scored differently across documents:**

  | Risk | Scores |
  | --- | --- |
  | Injection | R-FND-38 = 15, R-ID-06 = 15, R-VOX-09/R-MEM-15 = 20 |
  | Deletion | R-MEM-11 = 15, R-ID-04 = 20 |
  | Guest data | R-ID-07 = 16, R-VOX-19 = 15, R-MEM-16 = 12 |
  | Private note to wrong person | R-ID-52 = 10, R-MEM-06 = 15 |
  | Unauthenticated link | R-FND-20 = 8, R-ROB-10 = 15 |

- **Arithmetic error:** R-ROB-22 is "12 after mitigations with L3" at I5, but 3 x 5 = 15. That moves it into the high band, and the summary at line 47 repeats 12. (verified)
- **Correction:**
  - Risk IDs become `RK-<AREA>-<nn>`, with the area required to be alphabetic, and are globally unique.
  - Doc 50 owns each cross-cutting risk with one score; feature documents reference it.
  - Add `T-G<n>-<nnn>` and `T-X-01-<nnn>` to doc 00.

**M3. Gates G0-G10 are not designed; plan gate defects are missed.**
- Evidence: only doc 45 §18 designs a gate (G11). Elsewhere gates appear only inside card "acceptance links". No gate has its step 0 (`chaos --full`, soak, scans), a step-to-test map, a record template or a decision rule.
- Missed plan problems:

  | Gate | Problem |
  | --- | --- |
  | G1 step 6 | "say … " needs STT, which arrives at P2 |
  | G4 | "interrupt two replies" needs barge-in, which is P2-07 (after G4) |
  | G8 step 4 | needs `where_am_i` (P4-07, after G8) |
  | G9 step 3 | a 3 s lens cover while walking moves about 1.5 m, more than MAX_JUMP_M 0.5 m. By doc 40's own rule (line 645) guidance never resumes, so G9 fails as designed |
  | G5 | doc 30 asks 10 attacks per type, the plan 3; not listed in doc 30 §15 |
  | G10 | rules undefined when spatial/body/cost fail but identity/conversation pass; doc 45 T-R-01-004 claims to encode them "exactly" |

- Correction: add a gate section per document, or make doc 50 own all gates, and record these six plan defects.

**M4. Doc 00 §7's "score 15 or more needs an automated test before merge" cannot be met and is already broken.**
- Evidence:
  - Merged cards carry R-FND-06 (16, "no automated test can close it"), R-FND-12 (15) and R-FND-18 (16), with only Planned or Manual tests.
  - Physical and biometric risks (R-ROB-01, R-ID-01) are inherently gate-measured.
- Correction: add a waiver class: "automated where possible; otherwise a named gate procedure plus an owner's signed residual". List current breaches in each card's §12.

**M5. Statistics: several methods are wrong or misleading.**
- **Doc 30 line 99:** "session-level bootstrap … the wider of the two is quoted".
  - With zero events every resample is zero, so the bootstrap interval is [0,0]. The independence-assuming Clopper-Pearson always wins, and the dependence correction never applies where it matters.
- **Doc 30 line 101:** "below 1.5 percent per trial overall" from 200 impostor trials is unsupportable.
  - There are 4 people, 12 ordered pairs and few sessions. With a modest intra-cluster correlation (0.3), effective n is about 35, giving a bound of about 8.6%.
  - "Say, 40 trials" per pair is invented: 200 trials over 12+ pairs is about 17 per pair, giving a bound of about 17%.
  - Line 537's "0 of 40 G5 trials bounds below 7.5%" counts genuine presentations by 2 people in one sitting, which is not an impostor rate.
- **Doc 30 lines 94, 481-484:** "APCER" is defined as attacks accepted under the attacked person's name. In ISO/IEC 30107-3 that is IAPMR; APCER/BPCER apply to a PAD subsystem, which v1 does not have.
- **Doc 30 T-P3-09-006:** logistic/LLR calibration diverges on perfectly separated data, which is exactly what the zero-overlap thresholds aim for. The result is infinite LLRs fed into fusion. ECE over 10 bins with about 50 genuine trials is noise.
  - Correction: a regularised or PAV fit with pseudo-counts, plus a named LLR cap constant.
- **Doc 30 T-P3-09-011:** expects about 95% coverage of Clopper-Pearson intervals. CP is conservative; exact coverage is 98-99% at these n, so a correct harness fails.
  - Correction: assert coverage at least 0.95 minus Monte-Carlo tolerance.
- **Doc 30 line 91** mixes one-sided (3/n) and two-sided CP (about 3.7/n). Pick one.
- **Doc 45 T-R-02-013 (line 756):** "under 500 ms (p99)" from 50 trials.
  - A p99 cannot be estimated from 50 trials, and a p99 on a hard safety limit allows a 1% violation rate, contradicting T-R-02-020 ("never exceeded").
  - Correction: require the maximum under the limit on every trial, with n at least 59.
- **Checked and correct:**
  - Doc 20 Wilson table: 19/20 gives 76-99; 9/10 gives 60-98; 0.95^59 = 0.048; 0.9^29 = 0.047; Poisson 3/T.
  - Doc 45 rule of three: 1 - 0.05^(1/10) = 0.259.
  - Doc 45 transform worked example and the ROS-to-project matrix (determinant +1, yaw psi maps to heading psi).
  - Doc 30 CP intervals for 19/20, 8/10 and 0/10.
  - **Recommendation:** put one statistics standard in doc 00. It should cover the interval method, one- versus two-sided bounds, independence (n = sessions x pairs), the evaluation partition, and labelling small-n checks as "smoke, not a rate".

**M6. Undecided oracles and literal limits (doc 00 §3.1 and the brief's "named limits").**
- Weakest passages, quoted:
  - Doc 10: T-P0-05-047 "at most N log lines per minute"; T-P0-04-021 "pruned to N or documented"; T-P0-04-026 "over a stated size"; T-AND-012 "within a stated slope".
  - Doc 20:
    - T-P1-04-023 "… or a reservation system makes the total exactly under the cap (decide)";
    - T-P1-01-012 "more than N unsolicited per minute";
    - T-P2-05-016 "discarded or kept by the stated rule";
    - T-P2-07-009 "zero or bounded";
    - P1-05 §8.5 "Safe defaults (10 min, a conservative temperature)".
  - Doc 40: T-P4-05-019 "arrival requires N consecutive samples (to decide)"; T-P4-08-002 "status per decision G-45"; T-P4-02-013 "asserting the monitor does not claim ok-with-confidence … if the app reports it".
  - Doc 45: P5-02 "hourly and daily unprompted-turn caps" (unnamed); R-03 line 964 "each step's check written before it is done | process"; SR-14 "pinch points … acceptable (mechanical check)".
- Literal limits appear throughout docs 30 and 40, for example:
  - "20 regions per photo", "plus 25 percent", "aliases 8";
  - `CHECK length(statement) <= 300` while its value is still "to decide".
- Correction: every oracle cites a constant (proposed value allowed) or a decision ID that blocks the card's start.

**M7. Doc 10: claims about existing code that are wrong (verified unless noted).**
- **Overstate coverage:**
  - `test_empty_passphrase_refused` passes `"short"` (`test_migrate.py:50-52`); no empty passphrase is tested (T-P0-04-023, R-FND-26).
  - `test_guard_restores_the_previous_policy` (line 652) does not exist.
  - `test_python_dash_m_brain_runs_under_the_guard` (line 460) does not exist; the real name is `..._installs_the_guard_strips_proxies_and_refuses`.
- **Defect already fixed:** ArCheck "catch swallows the type" (line 259). `ArCheckActivity.kt:53` prints `e.javaClass.simpleName`. A real gap is missed instead: `session.close()` is skipped if `isDepthModeSupported` throws.
- **E3 "does not cover `__import__`" (line 36) is wrong:** `test_egress.py:357` covers it. The `pickle` and `shell=True` gaps are real; rate E3 Partial, not Good.
- **Wrong counts:**
  - 9 reserved tool names (lines 682, 690); there are 10, including `set_quiet`.
  - "8 mandatory Body members" (line 571); `base.py` has 10.
  - "Existing 380 tests" (line 923); there are 361.
  - Line 22 says "fix round open", but line 5 says it was committed in `bc7610c`.
- **Stale:** C-10 cites `docs/body_tools_egress_v0.md` §4 "item 8" as stale; it was already updated.
- **Not a contradiction:** C-8 (53 cards = 44 + 9).
- **Partly wrong:** F-14 calls named module constants "literals"; only some defaults are literals.
- **Confirmed (main defects):** F-01..F-13, F-15..F-17. F-06 and F-11 are incomplete:
  - `echo_test_result` logs the whole `data` with `%r` (`server.py:317-318`);
  - dict keys are not length-checked (`tools.py:408`);
  - post-auth websockets frames up to 8 MiB x `max_queue=4` sit in memory before the 64 KiB text check.

**M8. Feasibility on this machine is not addressed.**
- **Timers:**
  - On Python 3.13 / Windows ProactorEventLoop, `asyncio.sleep(0.001)` takes about 13-16 ms (measured by a helper).
  - These doc 20 thresholds therefore measure the event loop, not the code: T-P2-06-010 (30 ms), T-P2-05-012 (50 ms), T-P2-05-019 (20 ms) and T-P2-06-017 (1 ms).
  - Correction: use the fake clock for pass/fail; real timers report only.
- **Missing libraries:**
  - Not installed: Hypothesis, numpy, Pillow, psutil, onnxruntime, sounddevice, bandit, pip-audit and mutmut. The venv holds only cryptography, websockets, jsonschema, pytest and mypy (verified).
  - PB, ROC, JPEG, RSS-soak and mutation tests all depend on them.
  - Each needs a decision, plus a hash-pinned entry (B16, E10).
- **Mutation testing:** mutmut 3 is believed not to run natively on Windows. Doc 10 line 902 flags this as uncertain, but doc 00 §9 sets an 80% score at every gate regardless.
  - Correction: state the fallback (cosmic-ray, or a small hand mutator for the six security modules) in doc 00.
- **ROS 2 / Nav2:** the SIM sweeps in doc 45 need Linux. Nav2 does not support Windows, and WSL2 needs admin or IT approval on a returned company laptop. Doc 45 never says where SIM runs.
- **Tools:**
  - `tcpdump` (T-AND-011) is not available on Windows; firewall logging needs admin.
  - Creating symlinks (T-P1-03-017/025) needs admin or Developer Mode.
  - Airplane mode (T-P2-01-016) also kills the wireless-adb link the phone depends on.
- **Equipment** that may not be owned: a scope, a network emulator (netem is Linux-only), a mannequin, a calibrated loudspeaker. List them as gaps.

**M9. Privacy and consent gaps on a company-owned laptop (missing in all documents).**
- **Employer access:**
  - On a managed Windows 11 Enterprise device, a domain DPAPI backup key and BitLocker recovery escrow can let IT recover the registry key and disk contents.
  - Corporate backup or EDR may copy `data/`.
  - Windows crash dumps (WER LocalDumps can be disabled only under HKLM, which needs admin) and the pagefile can hold keys and templates.
  - Correction: add a risk (Privacy, I5) in doc 50, a decision on whether biometric data may live on this machine at all, and a test or check of the crash-dump setting.
- **Consent order:** doc 20 T-P2-07-013 ("by at least 3 speakers") and the T-P2-03-013 recorded sets collect family voices in Phase 2, but the consent machinery arrives in P3-01.
  - Correction: restrict Phase 2 to Jeff, or write an interim consent record.
- **Restore versus deletion:** doc 30 T-P3-01-046a says restore "re-applies the tombstones", but `tools/migrate.py restore_data` overwrites `registry.db` wholesale, resurrecting purged people.
  - Correction: keep tombstones outside the restorable set, and add a test.
- **Other subjects:** Syd's age is unknown, so guardian consent for a minor is not addressed (docs 30, 40). Non-family volunteers (T-P3-04-063) create third-party biometrics with no registry type or deletion path.

**M10. Doc 40 P4-08 and frames: logic errors and a code issue.**
- **Claim transitions:** table 8.3 has no transition out of `disputed`, so the plan's own acceptance (a spouse claim confirmed by Andrea's yes after dispute) cannot pass. `forgotten → previous` needs a stored previous status.
- **frame_id reuse (code issue, not flagged):** SimBody frame ids are `sim-1`, `sim-2` … and restart at 1 per instance (`sim.py:72,104`). The protocol example is `world-1`.
  - Persisted waypoints would match a new session's frame. T-P4-01-010 ("unique across 1,000 restarts") would fail today.
  - Correction: require globally unique frame ids (a UUID or boot id).
- **heading -pi (code defect, verified):** `Pose.heading_rad` documents the range (-pi, pi] but returns -pi for `quaternion_from_heading(-pi)` (`atan2(-1.2e-16, -1)`). Doc 40 T-P4-01-003 asserts the documented range without predicting the failure.
- **Missing safety risk:** there is no impact-5 risk for the person being guided (stairs, thresholds, moved furniture, watching the phone not the floor). R-SPA-36 (3x3) is the only safety line.

### Should fix

1. **Doc 00 §3, boundary list.** It has no boundary for the privileged console channel (the plan's "Console channel", whose authentication is "deliberately simple"). It also has none for model-bound tool results; B6 covers the model's output only. Add B17 Console and B18 Tool-result-to-model.
2. **Doc 00 §2, fail-closed rule.** "Nothing falls back to a weaker mode" conflicts with designed degradations: lite mode (P5-03), voice-only bands, and the local STT alternative. Distinguish security and authority fallback (forbidden) from functional degradation (allowed, if declared and tested).
3. **Doc 00 §5.1, negative classes.** Add classes for:
   - time-of-check/time-of-use (a file or registry changing between check and use; a capability changing mid-call);
   - error-message information leaks (paths, hostnames, stack traces in tool results returned to the model);
   - sleep/wake with open vendor streams;
   - upgrade and schema migration;
   - backup and restore;
   - accessibility of the voice interface (no visual fallback for "budget reached" or errors; children's and accented speech in STT and VAD sets);
   - documentation drift (docs naming tests that do not exist, as M7 shows: generate §12 "tests that exist" from `trace.py`, never by hand).
4. **"Stop always allowed" under failure is untested.** Nothing tests `stop` (and mute) when the P1-04 budget has hit 100% ("calls stop", plan line 678), the API is down, or the loop breaker has tripped. Add a local stop path that does not go through the model, and test it in docs 10, 20 and 45.
5. **Speech-vendor spend cap.** Doc 20 line 468 leaves it open, although plan risk 9 names it. Add per-vendor caps and a test.
6. **Log leakage of content.** No voice card tests that transcripts and TTS text stay out of the 10 MB x 3 rotating brain log (`__main__.py:82`) or the `-v` debug log ("payloads"). Add log-scan tests per card (S9).
7. **Per-tool boundary tests are missing** (doc 10 §7). None of the following is designed at below / at / above:
   - `capture_image.max_edge` 63/64/1568/1569;
   - `reason` 200/201;
   - `speak.text` 2000/2001;
   - `listen.timeout_s` 0.99/1/60/60.01;
   - `navigate_to.waypoint` 64;
   - `MAX_ARGS_NODES` 2000/2001.

   The tool-to-command handoff (tool `max_edge` up to 1568 against the protocol's 64..4096) is also untested. The 10 reserved names have no refusal tests. `pair --rotate`, `serve --no-phone-link`, `-v` and `body-check --wait` (NaN or negative) are untested.
8. **Protocol messages without brain-side negative tests:** `pose`, `tracking_state`, `image`, `audio_in`, `audio_out` (B3 quaternion and monotonic checks; image dimensions against bytes).
9. **Structure gaps:**
   - In doc 45, P5-05, P5-06, R-03, R-06, R-07, R-08 and R-09 are compressed: inputs table, self-checks, chaos, levels, registration and acceptance are mostly missing, and risks are written as prose. P5-02, P5-03, P5-04, R-01 and R-04 lack self-checking sections.
   - In doc 20, P1-05 has no named limits and no handoff column, and many "Monitor" cells say "Assertion in the test".
   - In doc 10, P0-01..03 have no handoff tables, and §0.2 is a placeholder.
   - For the optional and robot cards this compression is acceptable. Say so explicitly, rather than claiming the §8 structure.
10. **Doc 40 inconsistencies:**
    - `limited` both allows guidance (G-5) and pauses it (I1).
    - The stale boundary at exactly 500 ms is inconsistent.
    - T-P4-03-021's app allowlist "exactly {save_waypoint, list_waypoints, route_to, describe_place}" is wrong; the app channel also has `get_pose`, `recall` and others.
    - The T-P4-02-012 reset signature is invented; G0 showed a map-identity switch, not a snap to origin.
    - "G-8"/"G-9" gap IDs read as gates.
11. **Doc 30 practical points:**
    - Print attack photos at home, not on a company printer, and shred them.
    - Common Voice terms forbid trying to identify speakers, and `client_id` is not reliably one speaker; prefer LibriSpeech (CC BY 4.0).
    - The T-P3-01-020 "entropy above 7.9 bits/byte" check fails small genuine ciphertexts (1 KiB of random bytes averages about 7.8).
12. **Doc 45 watchdog and robot details:**
    - Windows has no SIGSTOP (line 756).
    - The e-stop pass criterion "within the recorded value" is circular (T-R-02-005).
    - The R-01 kit criterion should require modifiable firmware or an external relay watchdog, because the L1 design assumes reflashable firmware.
    - Impacts of 4 and 3 for "motors left running" and "fingers" are below doc 00's anchor of 5.
13. **Doc 20 hearing-safety impact** (R-VOX-05, "extremely loud audio") is 4; doc 00's anchor for harm to a person is 5.
14. **Missing operational risks:**
    - blocking SQLite on the asyncio loop stalling the 1 Hz guidance tick;
    - no backup schedule or restore test for the waypoint and memory stores;
    - Humble-to-Jazzy or map reload should invalidate the safety-config hash;
    - no test that no tool maps a person to a motion goal ("go to Syd").
    - Note: `who_is_present` is a plain body tool; the robot would need the check.

### Minor

- **Doc 20:**
  - Line 51 cites "Doc 00 section 9" for confidence intervals; it is §5 (ML row).
  - Line 639 has `stop_audio_out` for `stop_audio_in`.
  - T-P1-02-040's refusal reason is the identity check (`tools.py:546`), not "app channel".
- **Doc 45:**
  - Line 57 cites "section 4.2, q3"; it should be protocol §10 q3.
  - The line 588 table has 3 header columns and 4 separators.
  - Line 47 names R-ROB-14 for R-06; it should be R-ROB-27.
  - The R-RLZ prefix is used for non-relocalization risks.
- **Doc 40:** the G-3 claim "about 0.06 deg" error from a 1e-3 norm error is wrong; the maximum is about 0.115 deg.
- **Doc 10:** the Kotlin constant-time compare question (line 115) can be closed (`MessageDigest.isEqual`, `LinkAuth.kt:37`).
- **Doc 10:** T-P0-05-040 (1,000 x 8 MiB echoes, about 8 GB) belongs to full mode only.
- **Docs 20 and 40:** first-person author voice ("I did not read") is left in. Replace it with "Unverified".
- **Doc 10:** `test_typing` `skipif` when mypy is missing fails open (correctly flagged). Make it fail.

## D. Coverage summary (dimension 1)

| Item | Status |
| --- | --- |
| Cards P0-01..P0-07, X-01 | All present. 12 parts complete except P0-02/03 (thin: chaos, handoffs) |
| P1-01..P1-05, P2-01..P2-07 | All present. P1-05 thin; P2-04..07 merge acceptance and gaps |
| P3-01..P3-11 | All present with all 12 parts (best structured) |
| P4-01..P4-08 | All present with all 12 parts |
| P5-01..P5-06, R-01..R-09 | All present. P5-05/06 and R-03, R-06..R-09 compressed; see Should fix 9 |
| Gates | G11 designed. G0-G10 only through acceptance links (M3) |
| B1-B16 | All applied somewhere. B12 (Android intents) only in doc 10 §1; P5-02/P5-03 Kotlin parts lack B12/K-rules. No console boundary (Should fix 1) |
| 8 body tools | Generic coverage via existing `test_tools.py`. No per-tool limit tests (Should fix 7) |
| Brain-side tools | `save_waypoint`, `list_waypoints`, `where_am_i`, `route_to`, `describe_place`, `remember`, `recall` in doc 40; `start_enrollment`, `delete_person_data` in doc 30. `set_quiet` (reserved in `tools.v0.json`, class "motion") is only mentioned. `describe_place` and `set_quiet` are in the catalogue but not in the plan's brain-side tool list: a plan inconsistency nobody flagged |
| Protocol message types (14) | hello, auth, heartbeat, echo, error, event, command, ack covered. pose, tracking_state, image, audio_in, audio_out weak on the brain side |
| CLI | info, serve, probe, pair, body-check covered. Flags `--rotate`, `--no-phone-link`, `-v`, `--wait` not covered |
| X-01 matrix | OWASP LLM/Agentic, WebSocket, MASVS, 30107-3 (but see APCER, M5), chaos, mutation, supply chain, privacy and deletion, soak and compatibility are all designed in doc 10 §9 |

## E. Claim checks done directly by the reviewer (sample of 25)

| # | Claim (doc:line) | Verdict |
| --- | --- | --- |
| 1 | 361 Python tests; per-file counts 5/5/9/1, 58/42/15, 66/54/44/19/21/18/4 (10:5, 20-22) | Confirmed (collect-only) |
| 2 | 25 Kotlin @Test, 4 + 21 (10:72) | Confirmed (grep) |
| 3 | `assert` at protocol.py:121 and tools.py:407…559 (10:35) | Confirmed |
| 4 | `tools.py:509` deliberate swallow (10:37) | Confirmed |
| 5 | F-10 `log_n` 14..22 accepted before AEAD, `read_bytes` (10:58) | Confirmed (`migrate.py:101-112`) |
| 6 | F-11 caps 65,536 per string, 2,000 nodes, total checked only by final `json.dumps` (10:59) | Confirmed (`tools.py:390-426`) |
| 7 | Hypothesis, bandit, pip-audit absent; lower-bound deps, no lockfile (10:40, 43) | Confirmed |
| 8 | mypy strict on body, agent, egress only (10:39) | Confirmed (`pyproject.toml`) |
| 9 | `limits.py` does not exist (10:20) | Confirmed |
| 10 | Anchor drift NOT RUN, G0 recorded GO (10:19) | Confirmed (G0.md:3, 21: "GO with adjustments") |
| 11 | `test_empty_passphrase_refused` tests empty (10:363) | Wrong ("short") |
| 12 | Lint misses `__import__` (10:36) | Wrong (`test_egress.py:357`) |
| 13 | ArCheck catch swallows the type (10:259) | Wrong (`ArCheckActivity.kt:53`) |
| 14 | `test_guard_restores_the_previous_policy` and `test_python_dash_m_brain_runs_under_the_guard` exist | Wrong (not collected) |
| 15 | 9 reserved names (10:682) | Wrong (10) |
| 16 | Suite egress guard denies all real calls (20:67) | Wrong (allows `api.anthropic.com`) |
| 17 | `heading_rad` CCW from above, 0 facing -z (40:67; 45:103) | Confirmed (`types.py:171-178`, `v0.md:162`) |
| 18 | Plan says heading clockwise (40:G-1) | Confirmed (correctly flagged) |
| 19 | Doc 45 transform worked example gives (2, 1.4, -4) and 90 deg | Confirmed (hand computation) |
| 20 | R-ROB-22 scored 12 with L3, I5 (45:1244) | Wrong (15) |
| 21 | Doc 20 Wilson 19/20 gives 76-99; 9/10 gives 60-98; 59 and 29 zero-failure trials | Confirmed |
| 22 | G11 rule of three, 26% at n=10 (45:63) | Confirmed (1 - 0.05^0.1 = 0.259) |
| 23 | T-P3-09-005 bound on held-out data (30:101, 936) | Wrong (same set used for choice) |
| 24 | T-P4-05-004 oracle consistent with T-P4-05-001 (40:519-522) | Wrong (sign) |
| 25 | `tools.v0.json`: 8 tools + 10 reserved, incl. `describe_place`, `set_quiet` | Confirmed |
| 26 | Heading range (-pi, pi] in code (40:92) | Code wrong at -pi (verified) |

The helper passes recorded a further ~85 checks: about 75 confirmed, about 12 wrong, a few unverifiable (model-card dimensions and licence terms). The wrong ones are reflected in M7 and the Should fix and Minor lists.

## F. Usability for one developer (dimension 7)

**Volume.**
- About 1,320 test-table rows (about 1,700 IDs counting ranges) and 260 risk rows: doc 10 has 104 rows, doc 20 319, doc 30 212, doc 40 287, doc 45 397.
- That is roughly five planned tests for every test that exists today.
- At this size, documents written ahead of the code will drift (M7 already shows drift one day in), and they will be skimmed rather than followed.

**Cut or defer.**
1. Doc 45 R-02..R-09 detail (about 300 rows): keep R-02's safety rules, the stage ladder and the G11 statistics. Defer the test tables until R-01 says buy.
2. P5-05 and P5-06 (optional): one acceptance test each.
3. Collapse restated risks into doc 50 references.
4. Merge per-card chaos tables into the X-01 manifest instead of prose.
5. Make each card's section the source for its tests, and generate the "Existing" status by `tools/trace.py`. Never maintain it by hand.

**Prioritise.** Fix what protects money, data and people now (sections B1-B4), then the Phase 0 regressions, then P0-07 (the next card).

**Minimal "first 100 tests", in order.**

| # | Tests | Content |
| --- | --- | --- |
| 1 | 10 | Safety net: deny-all egress in conftest plus proof; mypy test fails if mypy missing; E2 assert lint and E4 except lint with baseline file; E3 add `pickle`/`shell=True`; secret scan of repo. |
| 2 | 16 | Phase 0 defect regressions: F-01 assert to raise; F-02/F-03 `pack_binary`/`unpack_binary` ranges; F-04 `Link.send` type; F-07 server params; F-08 CLI `--port`/`--bytes`/`--wait`; F-09 `load_config` schema and path confinement; F-10 `log_n` refused before scrypt plus truncation and bit-flip; empty/11/12-character passphrase; F-11 cumulative size and key length; F-06 event-key and `echo_test_result` log bounds; heading -pi. |
| 3 | 18 | Per-tool limits: 8 tools at below/at/above (Should fix 7), 10 reserved names refused, tool-to-command `max_edge` handoff. |
| 4 | 8 | Protocol: valid-frame rate limit, duplicate id no double dispatch, post-auth frame memory bound, B3 pose checks, shared Python/Kotlin message corpus (one file, both runners). |
| 5 | 3 | `limits.py` created; Python/Kotlin constants pinned; E5 references. |
| 6 | 15 | P0-07 agent with scripted hostile model: unknown tool, malformed JSON, oversize, loop budget 8, wrong-channel attribution, "never from app channel" phrases (corpus of about 20, one parametrised test), stop works with budget exhausted or API down, key never in logs or request log. |
| 7 | 10 | X-01 core: manifest drift both ways, quick runner seed replay, audit-hook monitor proven to fire, the five review regressions (recursion, log line, proxy bypass, log forgery, handshake lockout) planted and caught. |
| 8 | 10 | P1-04 cost: integer money, cap fail-closed at 100%, per-vendor caps, concurrent reservation, restart keeps the daily total. |
| 9 | 10 | Data custody: migrate round trip, restore verification, tombstone survives restore, log scan for transcripts and secrets, crash-dump setting check, frame-id uniqueness. |

## G. Doc 00 itself (dimension 8): summary

- **Sound:** the five promises, the boundary table, N-classes, risk method, fail-closed default, no real biometrics in the repo, seeded determinism.
- **Unenforceable as written:** E1 (M1); E5 without `limits.py`; E9 on every public function; the score-15 rule (M4); an 80% mutation score with no Windows-capable tool (M8); "every save" running E1-E8 plus mypy in 30 s (likely, but unmeasured).
- **Contradictory:** S1 versus S2 (if downstream takes gate types, re-checking every argument everywhere is redundant: S2 should be "check what the type does not guarantee"); fail-closed versus designed degradation (Should fix 2).
- **Missing:** an adoption baseline, a statistics standard, gate and X-01 test-ID formats, a console boundary, human factors and accessibility, observability (no test that logs carry the fields needed to debug a gate failure), and documentation-drift control.
- **Nothing dangerous in doc 00.** The dangerous items are in docs 40 and 45 (B1, B2).
