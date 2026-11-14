# Test and Risk Design: Overview and Standards

Status: draft v2, 13 Nov 2026 (corrected after the independent review). Owner: Jeff. This is the foundation document. The per-feature documents (`10-` to `50-`) apply it to every card in `EmbodiedAgent-Plan-and-Implementation-Order.md`. Where this document and a card disagree, raise it; do not silently pick one. The binding conventions C1 to C12 (`CONVENTIONS.md`) are carried here as canonical text; where a sentence below and `CONVENTIONS.md` disagree, `CONVENTIONS.md` wins and this document is fixed.

## Corrections applied (13 Nov 2026)

Source: `REVIEW.md` (sections B, C M1, M2, M4, M5, M6, M8, M9, Should fix 1 to 3, F and G) and `CONVENTIONS.md`. Every repository fact added below was checked against the code at `bc7610c` on this date.

- **Conventions C1 to C12 incorporated** as canonical text: test, risk, finding and decision IDs (8.1), environment codes (6), statistics (5.2), test layout and fakes (5, 6), limits (3.1, Appendix A), test-suite network policy (6), physical safety and deferral (17), Windows feasibility (14), oracles (8.2).
- **Status made honest.** `brain/limits.py`, `Limits.kt`, `require`/`ensure`, `@trusted_input`, the typed error hierarchy and the `tests/<area>/` folders do **not** exist. Each is now marked Planned with its module name (4.6, 5). Existing code that breaks the rules (E2, E4, literal limits) is listed as an adoption baseline, not ignored (4.5).
- **E1 redesigned** (review M1): the old "calls a validator" rule passed on one checked argument out of five and failed on every override and callback. It is replaced by a mechanical "no raw types past the gate" lint plus a gate manifest whose entries must have edge tests. A checked-in baseline with a burn-down rule lets the standard apply to new code on day one (4.4, 4.5). E2 to E10 now state how they are built, what exists today and their known limits (4.3).
- **Rules S1 to S12 and K1 to K7 tightened**: S2 no longer contradicts S1 ("check what the type does not guarantee"); S4, S10 and S12 are narrowed to the functions where they pay; each rule names its enforcement (4.1, 4.2).
- **Fail-closed rule split** into "security and authority never degrade" and "function may degrade if declared and tested"; stop and mute must always work without the model (2).
- **Boundaries B17 to B21** added (doc 45's marker, robot bus and safety controller; the console channel and tool results going back to the model). **Negative classes N17 to N21** added and N12, N13 extended (3, 5.1).
- **Risk method fixed** (review M2, M4): inherent score plus a residual score column; one canonical row per risk; a waiver class for risks scored 15 or more that no automated test can close (7).
- **Statistics standard** added (review M5; C5) (5.2).
- **New sections**: feasibility on this laptop (14), employer and device trust (15), consent timing and restore versus deletion (16), physical test safety and deferral (17), where to start (18), decisions created here (19).
- **Appendix A**: the canonical limits table, built from the code, the protocol schema and the proposals in docs 10 to 45, with conflicts decided and code-versus-spec differences listed.
- **Cadence made measurable** (9): the suite takes 49 s today, so the "under 30 s" quick suite needs a `slow` marker; mutation targets wait for a tool that runs on Windows.
- **Section numbers** 1 to 13 are unchanged (other documents cite 3, 3.1, 4, 4.1 to 4.3, 5, 5.1, 6 to 9). New material is in 4.4 to 4.6, 5.2, 8.1, 8.2, 14 to 19 and Appendix A.

## 1. Purpose and scope

The Embodied Agent listens, watches, recognizes family members, remembers what they say, speaks, and (later) moves. It runs on a company-owned laptop with a phone as its body, and it talks to a cloud language model. The brain host may change: the laptop is a company device that will be returned (sections 15 and 16, `docs/migration.md`, DEC-021), so the brain must stay portable and keep no state outside the migrated locations. Near-term work is the phone path (eyes, ears and mouth on the phone, the brain on the host; a person carrying the phone stands in for wheels, P4-06 and G9); the robot cards are parked (section 17). Mistakes here are not only crashes: a wrong name spoken confidently, a spoken command that deletes a file, a biometric template that leaks, or a loop that spends money are all failures.

This set of documents defines, for every feature in the plan:

1. how we prove the feature **does what it is supposed to do**;
2. how we prove it **does not do what it must not do**;
3. how we prove it **never operates on input it has not verified** (type, size, range, encoding, structure, origin);
4. how we prove it **never passes invalid data on** to another function, object, process or service;
5. how the functions we write **check themselves** before they operate on data;
6. what **risks** each feature carries, how likely and how bad, and which test addresses each.

It covers Python (brain), Kotlin (Android body), the wire protocol, the model-facing tool layer, the data stores, the build and release process, and the human-in-the-loop gate tests. It does not replace the independent reviews done at each card; it makes their findings permanent.

## 2. The five promises

Every feature is held to the same five promises. A feature is not done until each has at least one automated test or, where automation is impossible, a written manual procedure recorded at a gate.

| # | Promise | In one sentence |
| --- | --- | --- |
| P1 | Does the job | Valid input produces the specified output, within its stated time and resource limits. |
| P2 | Does only the job | No side effect outside the feature's declared allowlist: no file, process, network host, secret, or state change it was not given. |
| P3 | Verifies before use | Every input is checked for type, size, count, depth, encoding, range and pattern at its gate, before anything else touches it. |
| P4 | Passes only valid data | A value that failed a check is never forwarded. What leaves a function was validated, or is documented as untrusted. |
| P5 | Checks itself | Gate functions validate their arguments; functions that produce an invariant check it before returning; failures are loud and closed. |

**Fail-closed rule.** Two cases, kept apart:

- **Security and authority never degrade.** When a check that protects data, money, identity, the network allowlist or motion cannot be completed (missing file, corrupt token, library internals changed, dependency down), the action is refused or the process refuses to start. There is no weaker fallback.
- **Function may degrade, if declared and tested.** Lite mode (P5-03), voice-only confidence bands and a local speech-to-text alternative are designed degradations. They are allowed only when the card declares them, the user is told, a test covers the switch, and the degraded mode never gains authority (a degraded identity is never "confirmed").

**Stop always works.** `stop` and mute must work when the budget is exhausted, the model API is down, or the loop breaker has tripped, through a local path that does not go through the model (doc 50 C-16). Every card that adds speech, guidance or motion tests this.

## 3. Trust boundaries and what must be verified at each

A trust boundary is anywhere data enters from something we do not fully control. Each boundary has one validation point (a **gate function**) that parses raw input into a typed, size-limited object. Code beyond the gate accepts the typed object. The wire protocol is the model case: the schema plus the Python and Kotlin validators. Gate functions are listed in the gate manifest (4.4).

| ID | Boundary | Untrusted data | Minimum checks at the gate |
| --- | --- | --- | --- |
| B1 | Phone to brain, WebSocket text frames | JSON envelope and payloads | UTF-8 byte size, JSON parse with NaN and depth refused, closed envelope, schema, identifier patterns, integer-only ids and timestamps, sender direction, sequence, rate |
| B2 | Phone to brain, binary frames | Image and audio bytes | Header length and type, declared vs actual size, size cap, pairing with its JSON header, then media-level checks (decodable JPEG, dimensions, sample rate, duration) before any model sees it |
| B3 | Phone sensors | Pose, tracking state, battery, temperature | Finite numbers, plausible ranges, monotonic timestamps, quaternion normalisation, jump and speed limits |
| B4 | Microphone and camera content | What people say and show | Treated as data, never as commands. Content cannot widen a permission. Limits on length and rate |
| B5 | Speech-to-text and text-to-speech vendors | Transcripts, audio, error bodies | Size, encoding, control characters, language, vendor-error handling, retry limits, cost metering |
| B6 | Language model output | Text and tool calls | Treated as untrusted. Tool name allowlist, closed argument schemas, size and depth, loop and call budget, channel attribution from inputs not from the model |
| B7 | Tool arguments | IDs, names, short text | Types, lengths, patterns excluding path and command characters, normalisation, no free-form strings that can reach the filesystem, a shell or the network |
| B8 | Recognition outputs | Face and voice scores, embeddings | Finite values, vector length, score range, calibration present, quality gates, band thresholds from config only |
| B9 | Memory content | Claims people make, notes, descriptions | Length, character set, provenance fields present, status valid, content never re-enters the model as an instruction without framing |
| B10 | Files read | config.toml, .env, token, registry, enrollment media, recordings, digiKam metadata, migration archives | Existence, size, owner and permissions where relevant, parse errors handled, path confinement, schema version |
| B11 | Command line and environment | Flags, env vars, proxy variables | Types, ranges, allowlisted values, no secrets echoed, refused proxies |
| B12 | Android components | Intents, broadcasts, deep links | Export flags, required permissions, extras type-checked and bounded, debug-only paths absent from release |
| B13 | Network responses | Anthropic and speech vendor responses | Host allowlist, TLS, size caps, status handling, schema, timeouts |
| B14 | Time and clock | Wall and monotonic clocks, drift | Clock jumps, sleep and wake, offset estimation bounds |
| B15 | Operating system | Disk full, permission denied, process killed, network change, sleep | Handled without crash or unsafe state |
| B16 | Third-party code and models | Python and Gradle dependencies, model files | Hash-pinned, scanned, licence-checked, loaded from disk only, versioned |
| B17 | Visual marker and place photos (doc 45) | A printed marker or a room view, which an outsider can forge, move or copy | Closed marker ids, physical size consistency, two agreeing sightings, plausibility against the stored map |
| B18 | Robot bus (doc 45) | ROS 2 topics, services, actions, motor-controller link | Exact message types, finite values, stamps and rates, two-verb allowlist from the brain |
| B19 | Safety controller (doc 45) | Everything above it, including the brain | The brain is on the untrusted side; safety holds without it |
| B20 | Console channel | Jeff typing at the brain's stdin | The privileged channel: local stdin only (`Channel.CONSOLE`, `tools.py`); a remote console is not privileged; content from the app channel already in context makes the turn least-privileged |
| B21 | Tool result back to the model | Tool output carrying world content (image text, transcripts, notes) and error details | Size cap, framed as data, no paths, hostnames, stack traces or secrets in error text (N19) |

Every new boundary discovered later is added to this table in the same commit that adds the code.

### 3.1 Limits are named constants

- **One home.** Every size, count, depth, rate, time and tolerance limit is a named constant in `brain/limits.py` (Python) and, when the phone enforces it too, in `Limits.kt` (Android), with a comment giving its reason and owning card. **Neither file exists yet** (doc 50 H-11); until it lands, the constants in Appendix A stay where the table says they are enforced today.
- **Appendix A is canonical.** Documents use its names. Where two documents proposed different names or values, Appendix A decides; proposed values are marked Provisional and the owning card confirms them (by measurement or a recorded decision) when the code lands.
- **What is not a limit**: format constants fixed by a spec (the 20-byte binary header), physical definitions (battery 0 to 100 percent, quaternion components -1 to 1), and money caps chosen by Jeff (they live in `config.toml`, bounded by a sanity limit in `limits.py`).
- **Data files keep their copies.** Maxima that the model or the peer must see stay in `brain/agent/tools.v0.json` and `protocol/v0.schema.json`; a test pins each to its constant (extending the existing pins in `tests/test_protocol_schema.py`). Cross-language values come from one generated file, `protocol/limits.v0.json`, read by both test suites (H-11).
- **Edges are tested.** Tests import the constant and test one below, at, and one above it (E1b).
- **Literals are defects.** A numeric literal used as a bound in a validator is a defect. E5 finds them; existing ones are baseline entries (4.5) until H-11.

## 4. Self-checking function standard

These rules apply to code we write. Each names how it is enforced: an automated check from 4.3, or review at card end. What exists today is limited: the egress lint (E3), strict mypy on part of the tree (E6), the body-agnostic lint, and the validators already in `protocol.py`, `types.py` and `tools.py`. The helpers the rules refer to are Planned (4.6).

### 4.1 Rules (Python)

| Rule | Requirement | Enforced by |
| --- | --- | --- |
| S1 | **Parse, do not just validate.** A gate function turns raw input into an immutable typed object (a frozen dataclass whose `__post_init__` validates, or a `NewType` made only by the gate). Downstream functions accept that type, not `dict`, `Any` or raw `bytes`. | E1a, mypy (E6) |
| S2 | **Check what the type does not guarantee.** A function taking a gate-produced type does not re-check what the type guarantees; it checks only its own extra preconditions (ranges relative to other arguments, object state) with `require(...)`. A function that takes raw input from a gate it trusts says so with `@trusted_input("<gate>")`. | E1a, review |
| S3 | **Never use `assert` for validation or narrowing.** `assert` is removed under `python -O`. Use `if ...: raise` with a named error, or `typing.cast` when only narrowing a type the code has already checked. | E2 |
| S4 | **Check results where an invariant exists.** Gate outputs, geometry (normalised quaternions, wrapped angles), scores and money arithmetic check their invariant before returning, with `ensure(...)`. | Review; E1b tests |
| S5 | **Typed errors.** Expected failures raise a specific error (hierarchy in 4.6, Planned). No bare `except`; a broad `except Exception` only at a declared isolation point that logs and returns a typed failure (E4). Returning `None` for "no problem" is fine; returning `None` for a failure the caller must act on is not. | E4, review |
| S6 | **Bounded everything.** Every loop over external input, queue, buffer, retry, recursion and wait has a bound from Appendix A. Every network or process call has a timeout. | E5, review |
| S7 | **No dynamic execution of data.** No `eval`, `exec`, `pickle` of untrusted data, `shell=True`, string-built SQL, or format strings from outside. Paths from outside are never joined to a base without confinement checks. | E3, E7 |
| S8 | **Fail closed** (section 2). A missing or malformed validator, config value, key or file is refused with a clear message. Defaults are the safe value. | Tests per card |
| S9 | **Log safely.** Peer-supplied strings are logged with `%r` and truncated (`LOG_RECORD_MAX_BYTES`). Secrets, templates, transcripts and argument values are never logged. | Log-scan tests (PRIV) |
| S10 | **Side effects owned.** Network and process effects go only through their owning module (E3 enforces it for network and process). File writes outside `data/` and changes to global state are named in the docstring of the function that does them. | E3, review |
| S11 | **Resources released on every path.** Context managers or `finally`; cancellation safe. | Review; chaos tests |
| S12 | **Docstring contract, where it pays.** Gate functions and side-effecting functions document Inputs and limits, Returns and invariants, Raises, and Side effects. Tests are written from that contract. Other functions need a one-line docstring only. | E9 |

### 4.2 Rules (Kotlin)

| Rule | Requirement | Enforced by |
| --- | --- | --- |
| K1 | Parse raw `JSONObject`, `Intent` extras and bytes into immutable data classes at one gate; no raw maps beyond it. | Review; shared Python/Kotlin message corpus (H-02) |
| K2 | `require()` for argument checks and `check()` for state checks; no `!!`; never ignore a nullable. Today `!!` appears at `BrainLink.kt` lines 551 and 639 and `ClockOffset.kt` line 89 (H-21). | detekt `UnsafeCallOnNullableType` (Planned, H-14) |
| K3 | At gates and for expected I/O failures, return a sealed result type; exceptions for programmer errors; every `catch` logs and acts. | detekt `SwallowedException`, `TooGenericExceptionCaught` (Planned, H-14) |
| K4 | Bounds on every buffer, queue and wait; timeouts on every socket; no unbounded retry. | Review; Appendix A constants |
| K5 | No raw secrets in logs, intents, notifications or backups. | Review; log scan (T-AND tests) |
| K6 | Debug-only code lives only in `src/debug` (today: `DebugCommandReceiver.kt`) and is proven absent from a release build. | Manifest and dex check, `tools/android_checklist.py` (Planned, H-14) |
| K7 | Android lint and detekt run on every build with the rules above; the unit tests run on every build. | Build config (Planned, H-14; neither is configured today) |

### 4.3 Automated enforcement

Each check has a test that proves it fires (a planted violation it must catch). Lints E1a, E2, E4, E5 and E9 live in one stdlib-only module, `tools/lint.py` (outside `brain/`, Planned), run by `tests/unit/test_lint.py`; they read the baseline in 4.5.

| Check | Enforces | How it is implemented | Status today | Known limits |
| --- | --- | --- | --- | --- |
| E1 No raw types past the gate | S1, S2, P3 | E1a: AST lint of public signatures in boundary modules. E1b: gate manifest plus edge tests. Detail in 4.4 | Planned | 4.4 lists false positives, false negatives and what it cannot catch |
| E2 No `assert` in product code | S3 | AST: any `ast.Assert` under `brain/` or `tools/` fails, unless in the baseline | Planned. Baseline today: 7 (`protocol.py:121`, a real validation; `tools.py:407, 412, 415, 419, 520, 559`, type narrowing). Fix: H-06 (batch 1), plus one suite run under `python -O` | Does not catch `if __debug__:` guards or checks removed some other way |
| E3 Dangerous call lint | S7, S10 | AST over `brain/`: imports of network and process modules only in allowlisted files; calls to `exec`, `eval`, `compile`, `__import__`, `import_module`, `os.system`, `exec*`, `spawn*`, connect calls | **Existing, partial**: `tests/test_egress.py` `test_only_the_chokepoint_imports_network_or_process_libraries` and the planted-case test `test_the_lint_catches_each_way_of_reaching_out` (`__import__` is covered). Gaps: `pickle`, `marshal`, `shelve`, unsafe `yaml.load`, `torch.load`, `shell=True` as a keyword to an allowed call (H-22; matters at P3 model loading). Does not scan `tools/` | Name-based: an alias through `getattr` or a re-exporting module escapes it; it proves where calls are written, not what runs |
| E4 No bare or swallowing `except` | S5 | AST: bare `except:` always fails. `except Exception`/`BaseException` fails unless the handler re-raises, or the line carries `# broad-except: <reason>` and the handler logs (`log.exception`, `log.error`, `log.warning`) and returns or raises; a handler that is only `pass` or `continue` always fails | Planned. Baseline today: 9 (`tools.py:326, 509, 535, 555, 600`; `egress.py:328, 347`; `server.py:207, 300`). Most are deliberate isolation points: the fix is the reviewed marker, not removal (H-24 for `tools.py:509`) | Cannot judge whether a marked catch is right; markers are counted and reviewed at card end |
| E5 Limits named and used | S6, 3.1 | (a) Every constant in `brain/limits.py` is imported by at least one product module and has E1b edge tests. (b) In boundary modules, a numeric literal other than -1, 0, 1, 2 used in a comparison, a slice bound, `range()`, or a `timeout=`/`max_*=` keyword fails unless baselined | Planned (needs H-11). Literal baseline today includes `protocol.py:122` (500), `server.py:56` (10.0, 30.0), `server.py:57` (16), `link.py:64` (10.0, 30.0), `server.py:95` and `client.py:60` (4), `tools.py:333` (600), `tools.py:386, 526` (40), `tools.py:532` (64), `migrate.py:105` (14, 22), `__main__.py:82` (10 MiB, 3), `auth.py:249` (15) | Heuristic: misses limits passed as variables computed elsewhere; flags harmless literals (offsets, indices) that need a baseline line |
| E6 Static typing | S1 | `mypy --strict` via `tests/test_typing.py` | **Existing, partial**: covers `brain/body`, `brain/agent`, `brain/egress.py` (`pyproject.toml` `[tool.mypy] files`). Fails open: `skipif` when mypy is missing (H-13). Not covered: `brain/transport`, `brain/config.py`, `brain/__main__.py`, `tools/` | Types only: an `int` out of range is well typed |
| E7 Security linter | S7 | Bandit over `brain/` and `tools/`; each accepted finding carries `# nosec B<nnn> <reason>` and a baseline line | Planned; Bandit is not installed (DEC-101 (DEC-DEV-DEPS)). Disable B101 (assert; E2 covers it) | Pattern-based, many false positives; finds known shapes only |
| E8 Entry-point drift | X-01 step 1 | `chaos --check-manifest`: every tool, message type, command, CLI flag, config key, Body method and Android component is in `tools/chaos/manifest.toml`, and every manifest record still exists (T-X01-002) | Planned. Existing analogues: `tests/test_protocol_schema.py` pins Python, Kotlin, schema and doc constants against each other (tests at lines 73, 106, 114, 123, 132, 139, 229); `tests/test_tools.py` checks the catalogue | Finds entries, not whether each has good tests |
| E9 Docstring contract | S12 | AST: each manifest gate and each function flagged as side-effecting (in the manifest) has the four contract headings | Planned (scope narrowed per review M1) | Presence only, not truth |
| E10 Secrets and dependencies | B16 | Secret scan of the tree and `git log -p` (stdlib regex for key prefixes, 64-hex tokens, `.env` lines); `pip-audit`; hash-pinned lockfile with `--require-hashes`; Gradle dependency verification | Planned (H-03; no lockfile, `pyproject.toml` has lower bounds only; `pip-audit` not installed) | Audit finds known issues only; a regex scan misses novel secret shapes |
| E11 Body-agnostic lint | Plan architecture | No phone words outside `brain/body/phone.py`; only `brain/body/` and `brain/__main__.py` import the phone body | **Existing**: `tests/test_boundary_lint.py` (5 tests, with planted cases) | Word list based |

### 4.4 E1 in detail: no raw types past the gate

**Gate manifest.** Gate functions are listed in the `[[gate]]` table of `tools/chaos/manifest.toml` (the X-01 manifest; the E1 commit creates the file with only this table). Each entry gives `name` (dotted path), `boundary` (B-id), `produces` (the type or `NewType` it returns) and `limits` (Appendix A names it enforces). Frozen dataclasses with a validating `__post_init__` (today `Pose`, `Image`, `AudioChunk` and the other `brain/body/types.py` types) are gate types; their constructors and `from_json` class methods are listed as gates.

**E1a: signature lint.** Scope: public functions and methods (no leading underscore, in a module without one) in `brain/transport`, `brain/agent`, `brain/body`, `brain/identity`, `brain/memory`, `brain/spatial`, `brain/voice`, `brain/recorder`, `brain/config.py`, `brain/__main__.py` and `tools/migrate.py`, excluding listed gates.

- A parameter fails when its annotation is missing, or is (or contains in a union) `Any`, `object`, `dict`/`Mapping` of any form, `bytes`/`bytearray`/`memoryview`, or a container of any of these.
- A parameter annotated bare `str` fails when its name is in the external-text vocabulary: `text`, `raw`, `payload`, `data`, `body`, `message`, `transcript`, `statement`, `description`, `name`, `label`, `query`, `path`, `url`, `host`, `args`, `frame`, `header`. Such text should arrive as a `NewType` made by a gate (`PersonName`, `Transcript`, `WaypointName`).
- Skipped: dunder methods other than `__init__` and `__call__`, properties, parameterless functions, `@overload` stubs, and methods overriding a method of a base class in a scanned module (the base signature is checked once; mypy enforces compatible overrides).
- A call to a manifest `NewType` outside its gate's module fails (a `NewType` is otherwise forgeable anywhere).
- Exception marker: `@trusted_input("<gate name>")` (from `brain/contracts.py`, a no-op at run time). The named gate must exist in the manifest. Markers are counted and ratcheted (4.5) and reviewed at card end; use it only where a function genuinely receives raw data that a named gate has already checked.

**E1b: gate tests.** For each manifest gate and each limit it lists, collected tests must carry `@pytest.mark.gate("<gate>", limit="<CONST>", edge="below" | "at" | "above")` for all three edges, plus one `edge="wrong_type"` test per gate. The gate's dotted path must import and each limit must exist in `brain/limits.py`. The check reads the collection data that `tools/trace.py` uses (doc 50 section 3.3).

**In practice.** A new public function `def save(name: str, data: bytes)` in `brain/spatial` fails with "`name`: bare str from outside; `data`: raw bytes". The fix is to take `WaypointName` and a gate-produced `PhotoBytes`, or to make `save` a listed gate with edge tests. Code inside a gate module may handle raw types freely.

**False positives**: outgoing builders that take our own dicts (`Link.send(mtype, payload: dict)`), JSON helpers, logging helpers whose `name: str` is internal. Remedy: typed outgoing message classes over time; until then a baseline line.

**False negatives**: raw data in containers not on the list (`tuple[str, ...]`, `list[str]`); `str` parameters with names outside the vocabulary; data reaching code through attributes, globals, closures or `Callable[..., Any]` callbacks; `typing.cast`; private (underscore) functions, which are never checked; Kotlin (K1 is review plus the shared corpus).

**Cannot catch**: a gate whose checks are wrong or incomplete (E1b proves edges only for the limits listed, so a missing limit is a review item); raw data used before the gate is called; time-of-check to time-of-use; semantic errors (wrong units, wrong sign) in well-typed values.

### 4.5 Adoption baseline and burn-down

New checks must not fail the build on day one or be ignored. Existing violations are recorded in `tests/lint_baseline.toml`:

```toml
[meta]
max_entries = 29          # example: set by --write-baseline; may only go down; raising it needs a stated reason
max_trusted_input = 0     # @trusted_input markers allowed in total
max_broad_except_markers = 9

[[entry]]
check = "E2"
file = "brain/transport/protocol.py"
symbol = "ProtocolError.__init__"   # qualified name, not a line number (lines move)
count = 1
fix = "H-06"                        # doc 50 section 4.A item
card = "X-01"                       # the card that may not be marked done while this entry remains
```

Rules:

1. A violation not in the baseline fails.
2. A baseline entry that no longer matches (the code was fixed) fails as stale until it is deleted, so the file shrinks as fixes land.
3. `count` for a symbol may not rise; `max_entries` and the marker counts may only fall.
4. The baseline is written once by `tools/lint.py --write-baseline`, in the commit that introduces a check; later additions only by hand, with a reason, at card-end review.
5. `tools/trace.py check` refuses to mark a card done while baseline entries naming it remain, unless a waiver exists (doc 50 section 3.5).

Initial content, verified on 13 Nov 2026: E2, 7 entries (H-06, batch 1); E4, 9 entries (marker or fix, H-24 and batch 1); E5 literals listed in the E5 row of 4.3 (H-11, H-01, H-12, batch 2); E1a, generated by the first run (expected: outgoing `dict` payloads in `brain/transport/link.py` and `brain/body/link.py`, message handlers, config dicts). Burn-down order follows doc 50 section 4.A batches 1 to 3.

### 4.6 Planned support modules

| Module | Contents | Status |
| --- | --- | --- |
| `brain/errors.py` | `AgentError` (root); `ValidationError(AgentError, ValueError)` so existing `except ValueError` still catches; `LimitExceeded(ValidationError)`; `Unavailable`; `Refused`; `ContractViolation(AgentError, RuntimeError)`. `NotSupported` is re-exported from `brain/body/base.py`, not redefined | Planned. Today errors are per module: `BodyError` and subclasses (`base.py`), `ProtocolError` (`protocol.py`), `AuthError`, `TokenStoreError` (`auth.py`), `RequestError`, `LinkClosed` (`link.py`), `EgressDenied(PermissionError)` (`egress.py`); `types.py` raises `ValueError`. New code uses `brain/errors.py`; existing classes are re-parented only when touched, after checking every `except` site in the same change |
| `brain/contracts.py` | `require(cond, message, *, error=ValidationError)`; `ensure(cond, message)` raising `ContractViolation`; `trusted_input(gate)` decorator | Planned. Not to be confused with `Body.require(capability)` (`brain/body/base.py:199`), which checks capabilities and stays |
| `brain/limits.py`, `Limits.kt`, `protocol/limits.v0.json` | Appendix A constants | Planned (H-11, doc 50 C-02) |

## 5. Test taxonomy

Each feature document selects from these. Homes follow C6: new tests go under `tests/<area>/`. **None of these folders exists yet**; the 361 existing tests sit flat in `tests/` and stay there until touched, mapped to IDs by `tests/trace_map.toml` (doc 50 section 3.2). Environment codes (ENV-0 to ENV-6) are in section 6.

| Level or type | What it proves | Home |
| --- | --- | --- |
| U Unit | One function against its contract, including every `require` and `ensure`; also the lints of 4.3 | `tests/unit/` |
| C Contract | The same checks pass against every implementation of an interface (every Body, STT engine, TTS engine, face and voice backend) | `tests/contract/` |
| G Golden / known-answer | Fixed inputs give fixed outputs, identical across Python and Kotlin (HMAC, frame layout, clock) | `tests/golden/`, Kotlin unit |
| PB Property-based | An invariant holds for generated inputs (round trips, monotonicity, never crashes, never exceeds a limit) | `tests/property/` (needs Hypothesis, not installed) |
| ST Stateful | Order-dependent behaviour holds for generated action sequences | `tests/stateful/` |
| F Fuzz | Malformed, mutated and random bytes and structures never crash or leak | `tests/chaos/` |
| D Differential | Two implementations agree (Python and Kotlin parsers; fast and reference algorithms) | `tests/golden/` |
| I Integration | Real components together on loopback, no phone | `tests/system/` (marker `integration`) |
| S System | The brain runs end to end against SimBody or ReplayBody | `tests/system/` |
| PH On-phone (was P) | Behaviour on the real phone, scripted through adb and logs | `tools/` scripts, results in `docs/gates/` |
| H Human gate | A person does something and records the numbers | `docs/gates/G<n>.md` |
| CH Chaos | Disturbances (disconnect, sleep, disk full, clock jump, kill) leave the steady state intact | `tests/chaos/`, X-01 runner |
| RT Red team | Spoken, typed, pictured and model-generated attacks are refused | `tests/chaos/` with `tests/corpora/` |
| SEC Security | Static analysis, dependency, secret, manifest and permission checks | `tests/unit/` (lints), gate step 0 |
| PRIV Privacy | Consent, deletion, minimization, retention, guest handling, log scans | `tests/privacy/` |
| ML Accuracy and spoofing | Recognition accuracy with intervals (5.2), presentation attacks, threshold regressions | `tests/ml/` and gates |
| PERF Performance and latency | Time to first audio, per-stage latency, memory, CPU, battery, heat | `tools/` and gates |
| SOAK Long run | No leak or drift over hours | X-01 full mode |
| COMPAT Compatibility | Old recordings, old protocol versions, schema migration, restore of old archives | `tests/compat/` |
| MUT Mutation | The tests would notice if the code were wrong | Gate time, scoped (DEC-102 (DEC-MUTATION-TOOL)) |

### 5.1 Standard negative-test classes

Every input a feature accepts is exercised with each applicable class below. A feature document lists which classes apply to which input and cites the test IDs.

| Class | Examples |
| --- | --- |
| N1 Wrong type | string for int, list for string, bool for int, float with integer value, bytes for str, None |
| N2 Oversize | one over the limit, ten times the limit, a gigabyte-scale claim that must not allocate |
| N3 Empty and undersize | empty, one character, zero length, header-only |
| N4 Structure and depth | deep nesting, huge key counts, duplicate keys, unknown fields, missing fields |
| N5 Encoding and text | invalid UTF-8, lookalike Unicode, control characters, bidirectional marks, trailing newline, very long single token |
| N6 Numeric edge | NaN, infinity, negative, zero, maximum integer, overflow, subnormal, integer-valued floats, -pi and +pi exactly |
| N7 Injection | path traversal, command strings, format strings, log forging, prompt injection, SQL-like text |
| N8 Order and sequence | message before hello, auth skipped, repeated hello, reorder, duplicate, gap |
| N9 Replay and spoof | replayed handshake, replayed audio, photo of a photo, relayed proof |
| N10 Concurrency | two bodies, two requests, race at reconnect, cancel during send |
| N11 Resource exhaustion | floods, slow readers, many connections, large uploads, log spam |
| N12 Timing | slow handler, timeouts, clock jump, sleep and wake (including sleep with an open vendor stream) |
| N13 State corruption | corrupt file, partial write, truncated archive, schema version skew, upgrade and schema migration |
| N14 Dependency failure | vendor down, slow, malformed, rate-limited; library internals changed |
| N15 Permission and environment | access denied, disk full, file locked, read-only folder, missing folder |
| N16 Authority | wrong channel, wrong band, missing identity, spoofed caller, content claiming authority |
| N17 Physical-world fault (doc 45) | sensor dropout, stuck frame, wheel slip, brown-out, pickup, delayed or duplicated motor command |
| N18 Time of check to time of use | a file, registry row or capability changing between check and use |
| N19 Information leak in errors | paths, hostnames, stack traces, argument values or secrets in an error, log line or tool result returned to the model |
| N20 Backup and restore | restore of an old archive, restore after a deletion (section 16), partial restore, restore onto a non-empty folder |
| N21 Accessibility | no visual fallback needed for "budget reached" or errors; children's and accented speech in STT and VAD sets |

### 5.2 Statistics standard

Applies to every pass-count claim in every document and gate record (C5).

1. **Intervals.** Clopper-Pearson exact, two-sided 95 percent, for every pass count. For zero failures in n trials, the rule of three: the failure rate is below 3/n (one-sided 95 percent). State which is used; do not mix them in one claim.
2. **Disjoint data.** Never quote an interval computed on the data used to choose a threshold. Choose thresholds on a calibration split; quote the bound on a disjoint held-out split; split by person and session, never by trial. With little data, use leave-one-session-out.
3. **Dependence.** Trials from the same person and session are not independent. The effective n is at most sessions x pairs, not trials. A session-level bootstrap is allowed for dependent trials, but it collapses to [0, 0] when there are zero errors: never present it then.
4. **Small n.** Any gate with n below 30 is a **smoke test**: its record says so and what it cannot show (for example, 0 of 10 bounds the failure rate only below about 26 percent).
5. **Hard safety limits** are judged on the maximum over every trial, never on a percentile: "the stop time stayed under the limit on all n trials", with n at least 59 for a 95 percent claim that fewer than 5 percent of runs exceed it (0.95^59 = 0.048).
6. **Terms.** For attacks accepted under the attacked person's name use IAPMR (ISO/IEC 30107-3); APCER and BPCER are rates of a presentation-attack-detection subsystem, which v1 does not have.
7. **Harness self-tests** of interval code assert coverage of at least 0.95 minus a Monte-Carlo tolerance (Clopper-Pearson is conservative; exact coverage is 98 to 99 percent at small n).
8. **Calibration** uses a regularised fit (pseudo-counts or pool-adjacent-violators) and caps log-likelihood ratios at `LLR_ABS_MAX`, because a plain logistic fit diverges on perfectly separated data.

## 6. Test data, doubles and environments

- **No real family biometric data or real voices in the repository, ever.** Tests use synthetic faces and voices generated in code, public datasets whose licences allow it (prefer LibriSpeech, CC BY 4.0; Common Voice terms forbid trying to identify speakers), or fixtures created at test time from random seeds. Real enrollment data lives only under `data/` (git-ignored, verified in `.gitignore`). Held-out evaluation sets are described by manifest and kept in `data/`. Consent timing: section 16.
- **Doubles** live in one package, `tests/fakes/` (Planned; C6), with these canonical names: `FakeModel` (scripted model turns at the agent level) and its hostile subclass `HostileModel`; `FakeAnthropic` (the SDK client surface, for metering and egress tests); `FakeStt`, `FakeTts` (injectable latency and errors); `FakeClock`; `LoopbackBody` (body side of a loopback link); `FakeFs` (disk full, permission denied); `FakeAdb`; `SynthMedia` (generated images and audio). Card-specific fakes (doc 30's `FakeFaceBackend`, `FakeVoiceBackend`, `FakeKeyStore`) live in the same package. Older names (`ScriptedModel`, `tests/fakes.py`) are retired. Today the only shared helpers are inside `tests/test_transport_server.py` and `tests/body_contract.py` (H-27).
- **Corpora** (versioned in `tests/corpora/`, Planned): injection and red-team phrases (spoken, typed, hidden in image text, in transcripts, in speaker names, in stored claims), Unicode and control-character strings, malformed frames, oversize and deep structures, handshake abuse sequences, bad config files.
- **Environment codes** (C4), in increasing cost. Use these everywhere; do not use E1 to E10 for environments.

| Code | Environment |
| --- | --- |
| ENV-0 | Pure function or unit |
| ENV-1 | SimBody and loopback, no phone |
| ENV-2 | ReplayBody on a recorded session |
| ENV-3 | Phone in the loop, scripted |
| ENV-4 | Human at a gate |
| ENV-5 | Robot on blocks |
| ENV-6 | Robot on the floor, supervised |

- **Network policy for the test suite** (C8). All tests run under a loopback-only egress policy, never the allowlist in `config.toml`. **Defect today:** `tests/conftest.py` installs `EgressPolicy.from_config(load_config())`, and `config.toml` allows `api.anthropic.com`, so a real model call from a test would go out once a key is in `.env` (REVIEW B4). Fix: conftest installs an empty policy (loopback only), and a test asserts that `api.anthropic.com` is denied in the normal suite. Anything that would contact a real vendor or the real model carries the marker `real_vendor`, is excluded from default pytest (`-m "not real_vendor"`), and runs only by explicit command in a separate process with its own policy and a spend cap (the guard cannot be widened once installed, `egress.py`).
- **Determinism.** Every randomised test prints its seed and can replay it. Time, randomness and network are injected. Pass or fail of a timing behaviour is decided on `FakeClock`; real timers only report (section 14).

## 7. Risk method

Each feature document has a risk table (C2). Format:

| Field | Meaning |
| --- | --- |
| ID | `R-<AREA>-<nn>`, unique across all documents, no suffix letters. Areas: FND (Phase 0), EYE (Phase 1), VOX (Phase 2), ID (Phase 3), SPA (Phase 4 spatial), MEM (P4-08 memory), RLZ (Phase 5 cards P5-01 to P5-06), ROB (robot cards and G11), SYS (cross-cutting, doc 50) |
| Risk | What can go wrong, stated as an event |
| Category | Security, Safety, Privacy, Correctness, Availability, Cost, Legal, Operational |
| L, I | Likelihood and impact, 1 (low) to 5 (high), **inherent** (before mitigation) |
| Score | L x I, inherent |
| Mitigation | The design control |
| Tests | Test IDs that prove the mitigation holds, and that would fail if it were removed |
| Residual | Residual L x I after mitigation, what remains, and who accepted it |

Rules:

- **One canonical row.** A risk has one row, in its owning card's document (cross-cutting risks in doc 50). Other documents write `See R-xx-nn`, never a second copy. The same risk has the same L, I and score everywhere.
- **Score bands.** 15 and above is high: before the owning card is marked done it needs an Existing automated test, **or a waiver**: a named gate procedure (ENV-4 to ENV-6) plus Jeff's accepted residual with an expiry date, recorded as a `[[waiver]]` in `docs/testing/status.toml` (doc 50 section 3.5). This waiver class exists because physical and biometric risks are inherently gate-measured and some built cards already sit above the bar (R-FND-06, R-FND-18, R-FND-12; doc 50 section 3.5). 8 to 14 needs a test or a documented manual procedure. Below 8 may be accepted with a note.
- A risk with no test is a gap and is listed in the feature document's gaps section. A risk found later (review, incident, gate) is added in the same change as its fix and its regression test.

Standard impact anchors: **5** = harm to a person or their data (wrong confident identity acted on, biometric leak, unsafe motion including pinch and collision, hearing-damaging audio, destructive action); **4** = permanent loss of data or trust, or unbounded spend; **3** = feature unusable or misleading; **2** = degraded or annoying; **1** = cosmetic.

## 8. What every feature document contains

For each card, in this order:

1. **What it does** (two lines) and its dependencies.
2. **Inputs and boundaries**: a table of every input, its source, trust level, type, limits (Appendix A names), and where it is validated.
3. **Must do** (P1): the behaviours to prove, as test cases with IDs.
4. **Must not do** (P2): forbidden effects, as test cases with IDs, and which monitor (audit hook, egress guard, log scan) catches them.
5. **Input verification** (P3, P4): the negative-test classes that apply to each input, with IDs, and the places where data is handed to another function, with the check that guards the handoff.
6. **Self-checking requirements** (P5): the gate functions (for the manifest), the contracts they carry, and which of E1 to E11 cover them.
7. **Failure and chaos cases**: disturbances, steady-state hypothesis, blast radius.
8. **Risks**: the table from section 7.
9. **Test levels and environment**: which taxonomy levels apply, with ENV codes.
10. **Chaos-harness registration**: the entry points to add to the X-01 manifest.
11. **Acceptance link**: the plan card's acceptance test, the gate it feeds, and metrics with thresholds (5.2).
12. **Gaps and open questions**, with DEC IDs for anything that blocks the card.

Optional cards (P5-05, P5-06) and robot cards (R-01 to R-09) may use a compressed structure (inputs, risks, acceptance), and say so.

### 8.1 Identifiers

- **Tests** (C1): `T-<card>-<nnn>`, the card ID with its dash kept, three digits zero-padded (`T-P0-05-014`, `T-P3-10-032`). Exceptions: X-01 is `T-X01-nnn`; Android tests belonging to no single card are `T-AND-nnn`; gate tests are `T-GATE<n>-nnn` (`T-GATE11-004`); robot cards R-01 to R-09 are `T-RB<nn>-nnn` (`T-RB02-031`) so they cannot be confused with risk IDs; system-level tests are `T-SYS-nnn`. Ranges such as `T-P4-02-010..016` are forbidden: list every ID, or define one family ID `T-P4-02-F01` for a table-driven family and state its row count. Each test names its ID, promises and risks in a `@pytest.mark.trace(...)` marker or its docstring's first line (doc 50 section 3.2); `tools/trace.py` builds the traceability matrix.
- **Risks**: section 7.
- **Findings** (C3): `<doc>-F<nn>` (`10-F03`, `45-F02`). Each document ends with a one-line old-to-new table for renumbered findings. Doc 00 has no findings of its own.
- **Decisions**: one merged register, `DECISIONS.md` (also doc 50 section 5), with stable IDs `DEC-001` to `DEC-124` grouped by theme. The IDs the documents first allocated (`DEC-<name>` here, `DEC-10-nn`, `DEC-20-nn`, `DEC-ID-nn`, `DEC-SPA-nn`, `DEC-MEM-nn`, `DEC-45-nn`) stay as aliases in parentheses, for example `DEC-080 (DEC-HEADING)`. Each blocks the card it names until Jeff answers.
- **Name clashes to read carefully**: promise `P1` versus card `P1-01`; rule S1 versus stage S1 (section 17); write "rule S1" and "stage S1" where both could be meant.

### 8.2 Oracles

No test oracle may say "N", "decide", "stated rule", "a stated size", "zero or bounded" or similar (C12). Each bound cites a constant in Appendix A (a Provisional value is allowed) or a `DEC-` ID that blocks the card. Angles use the **built** heading convention (`protocol/v0.md` section 4.4 and `Pose.heading_rad`: counter-clockwise positive seen from above, so a left turn is positive); the plan's conflicting wording is DEC-080 (DEC-HEADING).

## 9. Cadence and gates

Measured on 13 Nov 2026: the full suite is 361 items, 360 passed and 1 skipped (`test_migrate.py:104`, symlinks not permitted on this machine) in 49 s; the slowest test (`test_watchdog_fires_even_when_the_peer_stops_reading`) takes 10.5 s. The quick budget therefore needs a `slow` marker (any test over 1 s) from X-01 stage 1.

| When | What runs | Budget |
| --- | --- | --- |
| Every commit (pre-commit hook) | Tests not marked `slow` or `real_vendor`, lints E1 to E5, E9, E11, mypy | `QUICK_SUITE_MAX_S` (30 s), measured and reported |
| Before push | Full suite plus the quick chaos suite and the trace check | `PRE_PUSH_MAX_S` (120 s) |
| End of every card | The card's tests plus the full chaos suite, an independent review at the model level the plan names, and the chaos-registration check | minutes |
| Step 0 of every gate | `python -m brain chaos --full`, mutation testing on the security modules (once DEC-102 (DEC-MUTATION-TOOL) is settled), short soak, dependency and secret scans, Android checklist, then the hands-on script. Gate designs G0 to G11 (step 0, step-to-test map, record template, GO, ADJUST or NO-GO rule) are owned by doc 50 section 2 | up to an hour |
| After a major feature or architecture change | The same as a gate, with the red-team corpus against the real model at capped cost (`real_vendor`) | as needed |
| Periodically (weekly when active) | Full chaos plus longer soak and the dependency scan | overnight |

A gate cannot be passed while any invariant fails. A failure found at a gate becomes a permanent regression test before the gate is re-run. Security-marked tests may not skip at a gate (a skip is a failure there).

Provisional targets, tuned with measured data: mutation score on the security modules at least `MUTATION_SCORE_MIN` with every survivor reviewed; zero crashes and zero forbidden side effects across the seeded fuzz runs; soak growth within `SOAK_RSS_SLOPE_MAX_MB_PER_H`, `SOAK_TASKS_MAX_GROWTH` and the log limits (Appendix A, harness group). Every gate record carries the fields needed to debug a failure: commit, seeds, versions (Python, Android, app, ARCore, model id, SDK), and the 5.2 statistics statement.

## 10. Defect handling

1. Reproduce with a seed or a recording; save it as a failing test.
2. Fix in the smallest place that restores the promise (usually the gate function or contract, not the caller).
3. Add the class of the bug to the negative-test classes or corpus so its relatives are tried.
4. Add or update the risk entry (one canonical row).
5. If a review found it, record which reviewer and which card, so the card's confidence is not over-stated. A defect in a design document is fixed in the document and logged in its findings table.

## 11. Document map

| File | Covers |
| --- | --- |
| `00-overview-and-standards.md` | This document: standards, conventions, limits (Appendix A) |
| `CONVENTIONS.md` | Binding conventions C1 to C12 for the correction pass; carried here as canonical text |
| `REVIEW.md` | Independent review of docs 00 to 45 (13 Nov 2026) that the correction pass applied |
| `10-phase0-foundations.md` | P0-01 to P0-07 and X-01 |
| `20-phase1-phase2-eyes-and-voice.md` | P1-01 to P1-05, P2-01 to P2-07 |
| `30-phase3-identity.md` | P3-01 to P3-11 (face, voice, fusion, consent, privacy) |
| `40-phase4-spatial-and-memory.md` | P4-01 to P4-08 |
| `45-phase5-phase6-robustness-and-robot.md` | P5-01 to P5-06, R-01 to R-09, G11 (robot test tables Deferred, section 17) |
| `50-risk-register-and-traceability.md` | Cross-cutting risks, the de-duplicated risk index, gate designs G0 to G11, traceability and counts, remediation (H-, B-, C- items), the merged DEC register, the "first 100 tests" order, and the reconciliation report |
| `DECISIONS.md` | The merged decision register (DEC-001 to DEC-124) with a top-10 digest for Jeff |

Docs 10 to 50 were corrected on 13 Nov 2026 against `REVIEW.md` and `CONVENTIONS.md`, then reconciled across documents the same day (doc 50 section 7).

## 12. Maintenance

The feature documents are updated in the same change as the code they describe. A card is not done until its document section lists the tests that exist; the "Existing" status is generated by `tools/trace.py` from collected tests, never maintained by hand (the review found names of tests that do not exist one day after writing). Reviews of this set: after every phase, and whenever a new boundary, tool, protocol message or store is added.

## 13. Sources behind the method

Method and test types were cross-checked against: OWASP GenAI Security Project (LLM Top 10 2025; Top 10 for Agentic Applications), OWASP WebSocket Security Cheat Sheet and Web Security Testing Guide, OWASP MASVS and MASTG, ISO/IEC 30107-3 (presentation attack detection testing), ASVspoof evaluation plans, Principles of Chaos (principlesofchaos.org), NIST AI 600-1 and MITRE ATLAS, Hypothesis documentation for rule-based stateful testing, and PyPA guidance on dependency auditing. Secondary summaries were used only as pointers; thresholds quoted in vendor material are not relied on.

## 14. Feasibility on this machine

The laptop is a company-owned Windows 11 Enterprise device used without administrator rights. Checked on 13 Nov 2026 unless marked otherwise.

| Item | Fact | Rule |
| --- | --- | --- |
| Python | Venv Python 3.13.17. Installed: cryptography 50.0.2, websockets 17.4, jsonschema 4.26.0, pytest 9.2.0, mypy 2.4.1 | 3.13 is required (`pyproject.toml`: 1 microsecond monotonic clock on Windows) |
| Missing packages | Not installed: Hypothesis, numpy, Pillow, psutil, onnxruntime, sounddevice, Bandit, pip-audit, mutmut, cosmic-ray, networkx | Each is "to install" with a hash-pinned entry (DEC-101 (DEC-DEV-DEPS)). A test that needs a missing package fails at a gate rather than skipping |
| Timers | `asyncio.sleep(0.001)` took a median 16.0 ms (min 3.9, max 16.4; 20 samples) | C10: timing tests use a tolerance of at least `TIMER_TOLERANCE_MS` (20 ms) unless they use a high-resolution wait; pass or fail is decided on `FakeClock`, real timers only report. Doc 20 thresholds of 1 to 50 ms measured with real sleeps measure the event loop, not the code |
| Mutation testing | mutmut on native Windows is unverified (believed not to run) | DEC-102 (DEC-MUTATION-TOOL); no mutation-score target applies until a tool runs |
| ROS 2 and Nav2 | Need Linux. WSL needs administrator rights and is not available | Phase 6 integration testing assumes a separate Linux target (DEC-039 (DEC-LINUX-TARGET)); robot tables are Deferred (section 17) |
| Symlinks | Not permitted for this account (`test_migrate.py:104` skips) | Tests of symlink refusal build the archive member in memory (`tarfile.TarInfo` of type `SYMTYPE`) instead of creating a link on disk |
| Admin-only actions | Writing HKLM (crash-dump settings), firewall logging, packet capture (`tcpdump` is not available), driver installs | Tests use evidence the user can read: brain egress logs, phone-side logs over adb, reading (not writing) HKLM |
| Network faults | `netem` is Linux-only | Delay, loss and stall are injected in-process (a loopback relay or `FakeClock`-driven transport) |
| Airplane mode | Also drops the wireless adb link the phone depends on | Use USB adb for such tests, or simulate the network loss |
| Equipment | Not confirmed owned: oscilloscope, network emulator, mannequin, calibrated loudspeaker or sound-level meter | A test that needs one is marked with its equipment and is not counted as active until it is owned |
| Printing | Attack photos for presentation tests | Print at home, never on a company printer; shred after use |
| Suite time | 49 s for the whole suite today | Quick-suite budget needs the `slow` marker (section 9) |

## 15. Employer and device trust

**Facts** (checked 13 Nov 2026): the laptop is Microsoft Entra ID joined (`dsregcmd /status`: AzureAdJoined YES, DomainJoined NO). Not checked: which management, backup and endpoint tools are active. The classic Active Directory DPAPI domain backup key does not apply to an Entra-only device, but the employer may still be able to:

- read the disk offline, if the BitLocker recovery key is escrowed to Entra ID or Intune (common on managed devices);
- run code on the device (Intune scripts, endpoint-detection live response); code running in Jeff's session can read his files and unprotect his DPAPI secrets (the Windows credential store);
- collect crash dumps (Windows Error Reporting) holding process memory: keys, templates, audio. Disabling local dumps needs HKLM, so it needs an administrator;
- copy files through backup, OneDrive known-folder redirection or file collection; the pagefile and hibernation file also hold memory.

**Implications.**

1. Treat everything on this laptop as readable by the employer.
2. DPAPI and the token folder ACL (`brain/transport/auth.py`) protect against other local accounts, not against the employer. That is acceptable for the phone pairing token; it is not enough for family biometric data (DEC-020 (DEC-BIOMETRIC-HOST), DEC-022 (DEC-VAULT-KEY)).
3. Until DEC-020 (DEC-BIOMETRIC-HOST) is answered, no face template, voice template, enrollment media or recording of anyone other than Jeff is stored on this laptop (section 16).
4. Keys and templates are held in memory only while needed. Two checks belong to doc 50 (R-SYS-02): the data root is not under a synced folder (T-SYS-014), and the brain reports the crash-dump setting at start (read-only, T-SYS-018).
5. **Return process** (doc 50 R-SYS-01, C-12; `docs/migration.md`): inventory every data-bearing location (`data/`, `.env`, `config.toml`, the token folder `%LOCALAPPDATA%\EmbodiedAgent\secrets`, agent-session data under `%USERPROFILE%\.claude`, later the vault key); export and `migrate verify`; keep a second copy; restore drill on a second folder; rotate the Anthropic key and re-pair the phone (`pair --rotate`); delete; rely on BitLocker and the company wipe for SSD remnants (an overwrite is not a reliable wipe on an SSD). Do this before any biometric data exists (doc 50 Q1, now DEC-021).

## 16. Consent timing, restore and deletion

**Consent timing.** Phase 2 brings a live microphone and cloud speech-to-text, but the consent gate and the encrypted registry arrive in P3-01. Rule: **no voice or face of any person other than Jeff is recorded, stored, or sent to a vendor before the P3-01 consent gate and encryption exist** (and, on this laptop, before DEC-020 (DEC-BIOMETRIC-HOST) allows it). For tests:

- Tests that need several speakers or faces (for example doc 20's multi-speaker barge-in and the recorded segmenter sets) use generated voices and images, licensed public corpora, and Jeff.
- Live Phase 2 sessions are run by Jeff, push-to-talk (doc 50 B-20), when others are out of earshot. A session in which another person is heard or seen is deleted at session end, not kept.
- Syd's age is not recorded. If Syd is under 18, consent comes from a parent, with Syd's own agreement, recorded in `docs/privacy.md` (doc 50 C-14). Non-household volunteers are not enrolled until the registry has a type and a deletion path for them.

**Restore and deletion.** `tools/migrate.py restore_data` extracts the archive over the destination, so restoring an archive made before a deletion would bring the deleted person back (it would overwrite the registry wholesale). Rule: **deletion records survive restore.**

- Deletion records are an append-only log kept outside the restorable set, and are also carried in each archive.
- Restore merges the archive's log with the destination's log and re-applies every deletion in the union before the brain may start.
- Archives made before a deletion are listed in the deletion record as copies still holding that person's data.
- Proving test: export, delete a person, restore the old archive, and the person is absent from every store (doc 50 T-SYS-016, with T-SYS-017 for a non-empty destination; doc 30 T-P3-01-049 and T-P3-01-051 test the registry side).

## 17. Physical test safety and deferral

**Stages** (C9). Every procedure with a moving robot follows these stages:

- **S0** bench, motors unpowered;
- **S1** on blocks, wheels off the ground, nobody touches the wheels;
- **S2** on the floor in an empty room, with the e-stop in hand;
- **S3** on the floor with a person-presence test, only after S2 passes and with a held e-stop. Surrogates only, never a person in the path, until after G11.

Never hold a driven wheel (use a brake fixture or a clamped load). Never deep-discharge a lithium battery (use a bench supply). Floor steps in any card (including R-03) require the R-02 stage S1 exit. Test procedures are reviewed against R-02 before use, by a second person (doc 50 Q20, now DEC-033).

**Deferral** (C11). The R-02 to R-09 robot test tables are Deferred until R-01 approves buying a robot. The designs are kept, but they are not counted in active totals.

## 18. Using this set: where to start

The set holds about 1,860 test IDs (about 1,650 active and 215 deferred with the robot cards; doc 50 section 3.6) against 361 existing Python tests and 25 Kotlin tests, too many to follow line by line. To use it:

1. **Start with the "first 100 tests"** in doc 50, in its order: the safety net (deny-all egress in conftest, mypy failing when missing, the E2 and E4 lints with their baseline, E3 gaps, a secret scan), then the Phase 0 defect regressions, per-tool limits, protocol hardening, `limits.py`, P0-07, X-01 core, cost and data custody. The order comes from `REVIEW.md` section F.
2. **Deferral** (section 17) removes the robot tables from the active count; optional cards P5-05 and P5-06 carry one acceptance test each.
3. **Build from the card's section when the card starts**: it is the source for that card's tests. Risks restated elsewhere are `See R-` references.
4. **Let the tools keep the status**: `tools/trace.py` reports what exists; the lint baseline (4.5) shows the burn-down.

## 19. Decisions created here

Each blocks the named work until Jeff answers (8.1). The merged register is `DECISIONS.md` (and doc 50 section 5); the merged ID is given first, the original ID in parentheses.

| ID | Question | Recommendation | Blocks |
| --- | --- | --- | --- |
| DEC-080 (DEC-HEADING) | The plan says heading is "positive clockwise" (P4-01) and "left negative" (P4-05); protocol section 4.4 and `Pose.heading_rad` are counter-clockwise positive. Which is the basis? | Keep the built convention (counter-clockwise positive, left turn positive) for every oracle; fix the plan text editorially (doc 50 B-34, Q9, now DEC-080). A spoken "left or right" is a separate, derived quantity computed by one function | P4-01, P4-05, any oracle on angles |
| DEC-020 (DEC-BIOMETRIC-HOST) | May family face or voice templates, enrollment media, or recordings of anyone but Jeff exist on this company laptop at all? | No. Run P3-01 enrollment, and keep any such data, only on Jeff's own machine after migration; until then the system is Jeff-only (section 15) | P3-01 first enrollment; Phase 2 sessions with others present |
| DEC-022 (DEC-VAULT-KEY) | How is the key for encrypted recordings and the registry protected: DPAPI (credential store) or a passphrase entered at start? | Passphrase-derived key (scrypt, as `tools/migrate.py`) for anything biometric; DPAPI is acceptable for Jeff-only data on this laptop. Record which in `docs/migration.md` | doc 50 C-06 (first recording, P1-03), P3-01 |
| DEC-102 (DEC-MUTATION-TOOL) | Which mutation tool, given that mutmut on native Windows is unverified? | Try mutmut once in X-01 stage 1 (T-X01-006). If it does not run, use a small stdlib `ast` mutation runner (comparison flip, off-by-one at bounds, constant swap, deleted `raise`) on the six security modules (`tools.py`, `protocol.py`, `auth.py`, `server.py`, `egress.py`, `migrate.py`); cosmic-ray as a second option | Mutation step of gate step 0; `MUTATION_SCORE_MIN` |
| DEC-101 (DEC-DEV-DEPS) | Install the development tools the designs assume? | Yes, with a hash-pinned dev lockfile and a line in `docs/licences.md` each: Hypothesis, psutil, Bandit, pip-audit now (X-01 stage 1); numpy and Pillow with the media gate (P1-01); model runtimes at their cards | PB, fuzz, SOAK and E7/E10 tests |
| DEC-039 (DEC-LINUX-TARGET) | Where does Phase 6 simulation and ROS 2 integration run? | A separate Linux machine owned by Jeff, chosen at R-01; nothing robot-related on this laptop | R-04, R-05 test plans (Deferred) |

---

## Appendix A: canonical limits table

Status: **Existing** = in code with a name, at the place given; **Literal** = in code as an unnamed literal (E5 baseline until H-11); **Spec** = in `protocol/v0.schema.json` or the catalogue and enforced through schema validation, with no constant in code; **Provisional** = proposed here or in docs 10 to 45, to be confirmed by the owning card; **Deferred** = robot cards (section 17). `Kt` = also needed in `Limits.kt`. When `brain/limits.py` lands, existing names move there unchanged unless the row says "rename". Naming: names end in their unit (`_BYTES`, `_S`, `_MS`, `_US`, `_PX`, `_HZ`, `_M`, `_MPS`, `_DEG`, `_CHARS`, `_DAYS`) or are counts (`_MAX`, `_MIN`, `_N`); names already used in docs 10 to 45 are kept unless they conflicted.

### A.1 Transport and protocol (P0-05)

| Name | Value | Unit | Why | Card | Status | Enforced today |
| --- | --- | --- | --- | --- | --- | --- |
| `MAX_TEXT_BYTES` (Kt) | 65,536 | bytes (UTF-8) | text frame size, spec section 1 | P0-05 | Existing | `protocol.py:73`; `Protocol.kt:10` |
| `MAX_BINARY_BYTES` (Kt) | 8,388,608 | bytes | binary data per frame, spec section 5 | P0-05 | Existing | `protocol.py:74`; `BinaryFrame.kt:35` as `MAX_DATA` (rename) |
| `MAX_FRAME_BYTES` | `MAX_BINARY_BYTES` + 20 | bytes | websockets `max_size` after auth | P0-05 | Existing | `server.py:32` |
| `PRE_AUTH_MAX_BYTES` | 16,384 | bytes | frame limit before auth | P0-05 | Existing | `server.py:31` |
| `MAX_PENDING_HANDSHAKES` | 4 | connections | evict-oldest | P0-05 | Existing | `server.py:29` |
| `FIRST_FRAME_TIMEOUT_S` | 2.0 | s | idle sockets dropped | P0-05 | Existing | `server.py:30` |
| `HANDSHAKE_TIMEOUT_S` (Kt) | 10 | s | whole handshake, spec section 4.2 | P0-05 | Literal | `server.py:56` (`hello_timeout_s=10.0`); `BrainLink.kt:50` `HANDSHAKE_TIMEOUT_MS` |
| `WS_RECV_QUEUE_FRAMES` | 4 | frames | TCP backpressure, spec section 1 | P0-05 | Literal | `server.py:95`, `client.py:60` (`max_queue=4`) |
| `HEARTBEAT_MS` (Kt) | 2,000 | ms | spec section 7 | P0-05 | Existing | `protocol.py:80`; `BrainLink.kt:43` `DEFAULT_HEARTBEAT_MS` |
| `LIVENESS_TIMEOUT_MS` (Kt) | 6,000 | ms | three missed heartbeats | P0-05 | Existing | `protocol.py:81`; `BrainLink.kt:44` `DEFAULT_LIVENESS_MS` |
| `HEARTBEAT_MS_MIN`, `HEARTBEAT_MS_MAX` | 100, 60,000 | ms | hello setting bounds | P0-05 | Spec | schema `hello.heartbeat_ms`; `BrainServer` does not check its own `heartbeat_s` (H-12) |
| `LIVENESS_TIMEOUT_MS_MIN`, `LIVENESS_TIMEOUT_MS_MAX` | 300, 300,000 | ms | hello setting bounds; liveness must exceed heartbeat | P0-05 | Spec | schema; not checked against `heartbeat_s` (H-12) |
| `SEND_TIMEOUT_S` | 30 | s | a blocked send aborts | P0-05 | Literal | `link.py:64`, `server.py:56` |
| `BINARY_PAIR_TIMEOUT_S` | 10 | s | wait for the binary half of a message | P0-05 | Literal | `link.py:64`; not in the spec |
| `REQUEST_TIMEOUT_S` | 10 | s | `Link.request` default | P0-05 | Literal | `link.py:259` |
| `CLOSE_TIMEOUT_S` | 2 | s | close handshake wait | P0-05 | Literal | `link.py:317` |
| `CLOSE_REASON_MAX_CHARS` | 120 | chars | WebSocket close reason fits 123 bytes (ASCII reasons only) | P0-05 | Literal | `link.py:321` |
| `MAX_PEER_ERRORS_PER_MIN` (Kt) | 20 | errors/min | more is fatal, spec section 11 | P0-05 | Literal (default argument) | `link.py:65`; `BrainLink.kt:55` |
| `HANDLER_QUEUE` | 256 | messages | `busy` beyond | P0-05 | Existing | `server.py:27` |
| `HANDLER_QUEUE_MAX_BYTES` | 33,554,432 (32 MiB) | bytes | 256 x 8 MiB could hold 2 GiB (H-04) | P0-05 | Provisional | - |
| `MAX_ID` (Kt) | 2^32 - 1 | - | message ids | P0-05 | Existing | `protocol.py:75`; `BinaryFrame.kt:36` |
| `MAX_TS` (Kt) | 2^53 - 1 | us | timestamps | P0-05 | Existing | `protocol.py:76` (`unpack_binary` checks only `ts < 0`, `protocol.py:275`, H-19); `Protocol.kt:11` |
| `IDENTIFIER_MAX_CHARS` (Kt) | 64 | chars | `boot_id`, `session_id`, `frame_id`, `utterance_id`, `image_id` | P0-05 | Existing (in regex) | schema `ident`; `types.py:31`; `tools.py:71`; `Protocol.kt:46` |
| `NAME_MAX_CHARS` | 64 | chars | hello names, event names, tool names | P0-05 | Existing (in regex) | schema `name`; `types.py:32`; `tools.py:55` |
| `HELLO_SOFTWARE_MAX_CHARS` | 128 | chars | | P0-05 | Spec | schema |
| `HELLO_NAMES_MAX` | 64 | items | `capabilities`, `commands` | P0-05 | Spec | schema |
| `HELLO_VERSIONS_MAX` | 16 | items | | P0-05 | Spec | schema |
| `ERROR_MESSAGE_MAX_CHARS` (Kt) | 500 | chars | `error.message` | P0-05 | Literal | `protocol.py:122` (`[:500]`); schema |
| `ECHO_TEXT_MAX_CHARS` | 4,096 | chars | diagnostics | P0-05 | Spec | schema |
| `JSON_MAX_DEPTH` (Kt) | 32 | levels | explicit depth check; today deep nesting is caught only as `RecursionError` (`protocol.py:170`) | P0-05 | Provisional | - |
| `PEER_MSG_MAX_PER_S` | 100 | valid messages/s per connection | valid-frame flood (H-09); excess policy per doc 50 Q12, now DEC-104 | P0-05 | Provisional | - |
| `LOG_RECORD_MAX_BYTES` | 2,048 | bytes | one log line; holds a full `error.message` plus fields (doc 10 chose 2,048; doc 50 H-08 said 1 KiB: 2,048 wins) | P0-05 | Provisional | - |
| `PEER_LOG_LINES_PER_MIN_MAX` | 120 | lines/min per connection | log spam from peer-triggered events (T-P0-05-047) | P0-05 | Provisional | - |
| `TOKEN_BYTES` (Kt) | 32 | bytes | pairing token | P0-05 | Existing | `auth.py:26`; `LinkAuth.kt:17` |
| `TOKEN_MIN_BYTES` | 32 | bytes | the server must refuse a shorter token | P0-05 | Literal, differs | `server.py:57` accepts 16 or more (see A.9) |
| `NONCE_BYTES` | 32 | bytes | handshake nonce | P0-05 | Existing | `auth.py:27` |
| `ACL_TOOL_TIMEOUT_S` | 15 | s | `icacls` subprocess | P0-05 | Literal | `auth.py:249` |
| `CLOCK_MAX_FIT_PPM` (Kt) | 200 | ppm | drift fit sanity bound | P0-05 | Existing (rename from `MAX_FIT_PPM`) | `clock.py:8`; `ClockOffset.kt:138` |
| `CLOCK_STABILITY_MAX_MS` | 20 | ms | spec section 6; reported in the session summary, nothing fails | P0-05 | Literal | `link.py:338, 349` (text only) |
| `CLOCK_WARMUP_S`, `CLOCK_WINDOW_S` | 60, 60 | s | stability measurement | P0-05 | Literal (defaults) | `clock.py:115, 134` |
| `CLOCK_HISTORY_MAX` | 5,000 | samples | estimator history | P0-05 | Literal (default) | `clock.py:40` |
| `UNREACHABLE_STOP_MS` (Kt only) | 180,000 | ms | body gives up without a stable link | P0-05 | Existing | `BrainLink.kt:45` |
| `ECHO_TEST_MAX_COUNT` (Kt) | 10,000 | echoes | bounds the echo test on both sides | P0-05 | Existing on phone; Provisional in brain | `BrainLink.kt:46` `MAX_ECHO_COUNT`; brain `probe --count` unbounded (H-18) |
| `ECHO_TEST_MAX_BYTES` (Kt) | 1,048,576 | bytes | as above | P0-05 | Existing on phone; Provisional in brain | `BrainLink.kt:47` `MAX_ECHO_BYTES`; brain `probe --bytes` unbounded (`__main__.py:287`, H-18) |
| `ADB_LINK_RECHECK_S` | 60 | s | re-run `adb reverse`, spec section 1 | P0-05 | Existing twice (merge) | `phone.py:29` `LINK_RECHECK_S` and `__main__.py:32` `PHONE_LINK_RECHECK_S` |

### A.2 Body interface and tools (P0-06)

| Name | Value | Unit | Why | Card | Status | Enforced today |
| --- | --- | --- | --- | --- | --- | --- |
| `CAPTURE_MAX_EDGE_PX_MIN`, `CAPTURE_MAX_EDGE_PX_MAX` (Kt) | 64, 4,096 | px | `capture_image.max_edge` on the wire | P0-06 | Existing | `base.py:43` `MAX_EDGE_PX_RANGE`; schema; pinned by `test_body_command_argument_bounds_match_schema` |
| `TOOL_MAX_EDGE_PX_MAX` | 1,568 | px | the model-facing tool is narrower than the wire | P0-06 | Spec (catalogue) | `tools.v0.json:40`; handoff to the wire untested |
| `DEFAULT_MAX_EDGE_PX` | 640 | px | tool default | P0-06 | Existing | `body_tools.py:32`; `tools.v0.json:40` |
| `POSE_RATE_MAX_HZ` (Kt) | 30 | Hz | `set_pose_rate` | P0-06 | Existing (rename from `MAX_POSE_RATE_HZ`) | `base.py:44`; schema |
| `EVENT_QUEUE`, `POSE_QUEUE`, `AUDIO_QUEUE` | 256, 16, 64 | items | body stream queues (drop oldest) | P0-06 | Existing | `streams.py:25-27` |
| `QUAT_NORM_TOL` | 1e-3 | - | unit quaternion check, then renormalise | P0-06 | Literal | `types.py:163` |
| `POSE_WAIT_S` | 2.0 | s | `get_pose` wait | P0-06 | Existing | `body_tools.py:31` |
| `MAX_ARGS_BYTES` | 65,536 | bytes | tool arguments as JSON; also the per-string cap | P0-06 | Existing | `tools.py:73, 420, 424` |
| `MAX_ARGS_DEPTH` | 8 | levels | | P0-06 | Existing | `tools.py:74` |
| `MAX_ARGS_NODES` | 2,000 | values | | P0-06 | Existing | `tools.py:75` |
| `MAX_KEYS_SHOWN` | 5 | names | argument names in a log line | P0-06 | Existing | `tools.py:76` |
| `LOG_KEY_MAX_CHARS` | 40 | chars | one shown argument name or path part | P0-06 | Literal | `tools.py:386, 526` |
| `TOOL_NAME_MAX_CHARS` | 64 | chars | tool names (regex) and a refused name as shown | P0-06 | Existing (regex), Literal (`tools.py:532`) | `tools.py:55` |
| `TOOL_DEFAULT_TIMEOUT_S` | 15 | s | registry default | P0-06 | Existing (rename from `DEFAULT_TIMEOUT_S`) | `tools.py:72` |
| `TOOL_TIMEOUT_MAX_S` | 600 | s | catalogue sanity bound | P0-06 | Literal | `tools.py:333` |
| `LISTEN_BASE_TIMEOUT_S` | 10 | s | `listen` tool timeout before its argument is added | P0-06 | Spec (catalogue) | `tools.v0.json:104` |
| `LISTEN_TIMEOUT_S_MIN`, `LISTEN_TIMEOUT_S_MAX` | 1, 60 | s | `listen.timeout_s` (default 10) | P0-06 | Spec (catalogue) | `tools.v0.json:110` |
| `REASON_TEXT_MAX_CHARS` | 200 | chars | `capture_image.reason` | P0-06 | Spec (catalogue) | `tools.v0.json:42` |
| `SPOKEN_TEXT_MAX_CHARS` (Kt) | 2,000 | chars | `speak.text`, `speak_local.text`, and one spoken reply (replaces doc 20's `TTS_MAX_REPLY_CHARS` 1,500: one cap on one path) | P0-06 | Spec | `tools.v0.json:84`; schema `speak_local.text` |
| `WAYPOINT_NAME_MAX_CHARS` | 64 | chars | `navigate_to.waypoint`, `save_waypoint` (doc 40's `WAYPOINT_NAME_MAX`) | P0-06, P4-03 | Spec (catalogue) | `tools.v0.json:191` |
| `ABANDONED_TASKS_PER_TOOL_MAX` | 4 | tasks | a handler that ignores cancellation (H-20) | P0-06 | Provisional | - |

### A.3 Config, CLI, logs and migration (P0-04)

| Name | Value | Unit | Why | Card | Status | Enforced today |
| --- | --- | --- | --- | --- | --- | --- |
| `SCRYPT_LOG_N` | 17 | log2 | archive key derivation; restore must accept only this (H-01) | P0-04 | Existing; Literal 14 to 22 accepted | `migrate.py:30`; `migrate.py:105` |
| `PASSPHRASE_MIN_CHARS` | 12 | chars | archive passphrase | P0-04 | Existing (rename from `MIN_PASSPHRASE`) | `migrate.py:31` |
| `ARCHIVE_MAX_BYTES` | 8,589,934,592 (8 GiB) | bytes | restore refuses larger before reading (today `read_bytes()` loads it whole) | P0-04 | Provisional | - |
| `ARCHIVE_UNPACKED_MAX_BYTES` | 34,359,738,368 (32 GiB) | bytes | gzip bomb (T-P0-04-026); checked while extracting | P0-04 | Provisional | - |
| `CONFIG_MAX_BYTES` | 65,536 | bytes | `config.toml` (H-05) | P0-04 | Provisional | - |
| `ENV_FILE_MAX_BYTES` | 16,384 | bytes | `.env` (H-05) | P0-04 | Provisional | - |
| `LOG_FILE_MAX_BYTES`, `LOG_FILE_BACKUPS` | 10,485,760, 3 | bytes, files | rotating brain log | P0-04 | Literal | `__main__.py:82` |
| `LOG_FILES_MAX` | 20 | files | per-run log files kept in `data/logs` (T-P0-04-021) | P0-04 | Provisional | - |
| `RESOLVED_TTL_S` | 600 | s | addresses learned for a listed host | P0-06 | Existing | `egress.py:60` |
| `REQUEST_LOG_RETENTION_DAYS` | 14 | days | full-prompt request log is personal data (doc 50 B-10) | P0-07 | Provisional | - |

### A.4 Agent loop and cost (P0-07, P1-04)

| Name | Value | Unit | Why | Card | Status |
| --- | --- | --- | --- | --- | --- |
| `MAX_TURN_TOOL_CALLS` | 8 | calls | loop breaker (plan P1-04 step 7) | P0-07 | Provisional |
| `MAX_TURN_SECONDS` | 60 | s | one agent turn | P0-07 | Provisional |
| `MAX_MODEL_TEXT_CHARS` | 4,000 | chars | model text accepted per turn; spoken part capped by `SPOKEN_TEXT_MAX_CHARS` | P0-07 | Provisional |
| `TOOL_RESULT_MAX_BYTES` | 32,768 | bytes | tool result returned to the model (B21; doc 10's `MAX_RESULT_BYTES`, renamed). Tool-call JSON uses `MAX_ARGS_BYTES` (no separate `MAX_TOOL_JSON_BYTES`) | P0-07 | Provisional |
| `MODEL_STREAM_IDLE_S` | 30 | s | stream idle timeout | P0-07 | Provisional |
| `MODEL_RETRIES_MAX` | 1 | retries | | P0-07 | Provisional |
| `MAX_OUTPUT_TOKENS` | 1,024 | tokens | `max_tokens` on every request; bounds one request's cost | P0-07 | Provisional |
| `MAX_CONTEXT_TOKENS` | 32,000 | tokens | context cap; with `MAX_OUTPUT_TOKENS` bounds worst-case request cost (T-P1-04-013) | P1-04 | Provisional |
| `LLM_CONCURRENT_REQUESTS_MAX` | 2 | requests | in-flight cost overshoot bound (T-P1-04-023) | P1-04 | Provisional |
| Session and daily spend caps | Jeff's choice | money | live in `config.toml`, bounded by `SPEND_CAP_SANITY_MAX` (a typo cannot set a huge cap) | P0-07, P1-04 | Doc 50 Q8 |
| `SPEND_CAP_SANITY_MAX` | 100 | USD per day | refuses a cap above this at start | P1-04 | Provisional |

### A.5 Eyes and voice (P1, P2)

| Name | Value | Unit | Why | Card | Status |
| --- | --- | --- | --- | --- | --- |
| `MAX_IMAGE_BYTES` (Kt) | 2,097,152 (2 MiB) | bytes | a phone still (far below the wire's 8 MiB). Decides doc 20 (2 MiB) vs doc 30 (`IMAGE_MAX_BYTES` 8 MiB): phone stills use this; brain-side files use `MAX_IMAGE_FILE_BYTES` (doc 50 B-54, Q18, now DEC-121) | P1-01 | Provisional |
| `MAX_IMAGE_DIM_PX` | 4,096 | px per side | matches `CAPTURE_MAX_EDGE_PX_MAX`; the wire header allows 8,192 | P1-01 | Provisional |
| `MAX_IMAGE_PIXELS` | 16,777,216 | pixels | 4,096 x 4,096; decompression bomb checked from the header before decode | P1-01 | Provisional |
| `MAX_IMAGE_FILE_BYTES` | 8,388,608 (8 MiB) | bytes | brain-side image files (enrollment, digiKam) | P3-02 | Provisional |
| `MAX_IMAGE_FILE_PIXELS` | 50,000,000 | pixels | camera photos up to about 48 MP; decoded only in a size-limited worker; P3-02 lowers it after measuring the tagged library | P3-02 | Provisional |
| `UNSOLICITED_IMAGES_PER_MIN_MAX` | 10 | images/min | images never asked for; more closes the link (T-P1-01-012) | P1-01 | Provisional |
| `FRAME_CACHE_N` | 10 | frames | cached stills | P1-02 | Provisional |
| `FRAME_FRESH_US` | 500,000 | us | a still counts as current | P1-02 | Provisional |
| `STALE_NOTE_US` | 2,000,000 | us | older stills carry an age note | P1-02 | Provisional |
| `AUDIO_IN_RATE_HZ` (Kt) | 16,000 | Hz | the gate is stricter than the wire's 8,000 to 192,000 (plan) | P2-02 | Provisional |
| `AUDIO_IN_CHUNK_S` (Kt) | 0.080 | s | chunk length (2,560 bytes at 16 kHz mono) | P2-02 | Existing in SimBody (`sim.py:40` `CHUNK_S`) |
| `AUDIO_IN_MAX_CHUNK_BYTES` (Kt) | 5,120 | bytes | two nominal chunks | P2-02 | Provisional |
| `AUDIO_MAX_AGE_MS` (Kt) | 2,000 | ms | body drops older audio under stall (doc 50 B-17) | P2-02 | Provisional |
| `AUDIO_OUT_RATE_HZ` (Kt) | 24,000 | Hz | TTS output (plan P2-01 suggests 24 kHz) | P2-01 | Provisional |
| `AUDIO_OUT_MAX_CHUNK_BYTES` (Kt) | 48,000 | bytes | 1 s at 24 kHz mono s16le | P2-01 | Provisional |
| `PLAY_BUFFER_MAX_MS` (Kt) | 2,000 | ms | phone playback queue | P2-01 | Provisional |
| `PLAY_PEAK_MAX_DBFS` (Kt) | -3 | dBFS | sample peak cap; the app never raises stream volume | P2-01 | Provisional |
| `TTS_LEAD_MS` | 1,000 | ms | audio sent ahead of playback; a cancel drops about this much | P2-05 | Provisional |
| `TTS_MAX_SENTENCE_CHARS` | 300 | chars | one TTS request | P2-05 | Provisional |
| `STT_MAX_CHARS` | 2,000 | chars | one transcript (15 s of speech is far less) | P2-04 | Provisional |
| `UTTERANCE_MAX_S` | 15 | s | forced cut (plan P2-03; C7). Doc 30's 30 s is superseded | P2-03 | Provisional |
| `UTTERANCE_MIN_MS` | 300 | ms | segmenter minimum (plan) | P2-03 | Provisional |
| `END_SILENCE_MS` (default), `END_SILENCE_MS_MIN`, `END_SILENCE_MS_MAX` | 500, 100, 5,000 | ms | config default and bounds | P2-03 | Provisional |
| `PRE_ROLL_MS` | 300 | ms | (plan) | P2-03 | Provisional |
| `ECHO_TAIL_MS` (default), `ECHO_TAIL_MS_MAX` | 300, 1,000 | ms | half-duplex gate tail after playback (config) | P2-07 | Provisional |
| `WAKE_WORD_MIN_INTERVAL_S` | 2 | s | at most one wake-word event per interval (a TV repeating the phrase cannot start a burst of turns; doc 30 T-P3-08-032) | P2-07 | Provisional |
| `SESSION_TIMER_S` (Kt) | 600 | s | phone session timer default; the app channel cannot extend it | P1-05 | Provisional |
| `BATTERY_TEMP_MAX_C` (Kt only) | 40 | degrees C | phone stops capture above this, without the brain (DEC-066 (DEC-20-18)) | P1-05 | Provisional |

### A.6 Identity (P3; from doc 30 section 0, reconciled)

| Name | Value | Unit | Why | Card | Status |
| --- | --- | --- | --- | --- | --- |
| `FACE_MIN_WIDTH_PX` | 60 | px | plan P3-02 step 5 | P3-02 | Provisional |
| `FACE_BLUR_MIN` | 100 | Laplacian variance | start value; calibrated by the P3-02 quality test on Jeff's photos | P3-02 | Provisional |
| `FACE_EMBED_DIM` | 128 | floats | SFace (expected; checked on model load) | P3-02 | Provisional |
| `VOICE_EMBED_DIM` | 192 | floats | ECAPA-TDNN (expected; checked on model load) | P3-05 | Provisional |
| `EMBED_NORM_TOL` | 1e-3 | - | after L2 normalisation | P3-02 | Provisional |
| `FACES_PER_IMAGE_MAX` | 10 | faces | detector output | P3-02 | Provisional |
| `FACE_LANDMARK_MARGIN` | 0.25 | fraction of box | landmarks inside the box plus 25 percent | P3-02 | Provisional |
| `XMP_PACKET_MAX_BYTES` | 1,048,576 (1 MiB) | bytes | digiKam metadata | P3-03 | Provisional |
| `FACE_REGIONS_PER_PHOTO_MAX` | 20 | regions | digiKam tags per photo | P3-03 | Provisional |
| `FACE_TEMPLATES_MIN`, `FACE_TEMPLATES_MAX`, `FACE_LIVE_MIN` | 8, 15, 3 | templates | plan P3-03 | P3-03 | Provisional |
| `FACE_SMOOTH_K`, `FACE_SMOOTH_N` | 2, 3 | frames | 2 of 3 (plan P3-04) | P3-04 | Provisional |
| `VOICE_MIN_SPEECH_S` | 1.0 | s | plan P3-05 | P3-05 | Provisional |
| `VOICE_ENROLL_NET_MIN_S`, `VOICE_ENROLL_FAIL_BELOW_S` | 40, 20 | s | plan P3-06 | P3-06 | Provisional |
| `SPEAKER_WAIT_MS` | 150 | ms | plan P3-07 | P3-07 | Provisional |
| `INHERIT_WINDOW_S` | 10 | s | plan P3-07, P3-10 | P3-07 | Provisional |
| `MIXED_WINDOW_S` | 1.5 | s | plan P3-08 | P3-08 | Provisional |
| `MEDIA_STATE_ONSET_MAX_S` | 20 | s | plan P3-08 | P3-08 | Provisional |
| `LLR_ABS_MAX` | 4.6 | natural log | ln(100): no modality claims better than 100:1 (5.2); doc 30 also clips to ln(n/3) of the evaluation impostor count | P3-09 | Provisional |
| `FACE_EVIDENCE_MAX_AGE_S` | 2 | s | plan P3-10 | P3-10 | Provisional |
| `DISPLAY_NAME_MAX_CHARS` | 40 | chars | person names (`PersonName`; today `Speaker.name` is unchecked, H-16) | P3-01 | Provisional |
| `PERSONS_MAX` | 32 | persons | a household | P3-01 | Provisional |
| `QUARANTINE_DAYS` | 30 | days | staged delete (plan) | P3-01 | Provisional |
| `PERMISSION_REASK_DEFAULT_DAYS` | 7 | days | plan P4-08 | P4-08 | Provisional |

### A.7 Spatial and memory (P4; from doc 40 section 0, reconciled)

| Name | Value | Unit | Why | Card | Status |
| --- | --- | --- | --- | --- | --- |
| `POSE_RATE_HZ` | 10 | Hz | default pose rate (spec section 4) | P4-01 | Provisional |
| `POSE_HISTORY_S` | 60 | s | plan P4-01 | P4-01 | Provisional |
| `POSE_STALE_MS` | 500 | ms | over this the verdict is lost (plan P4-02) | P4-02 | Provisional |
| `MAX_SPEED_MPS` | 3.0 | m/s | plan P4-02 | P4-02 | Provisional |
| `MAX_JUMP_M` | 0.5 | m | plan P4-02; measured against dt and the last good pose (doc 50 B-35) | P4-02 | Provisional |
| `MIN_DT_US` | 20,000 | us | below this no speed is computed (doc 40 suggestion) | P4-02 | Provisional |
| `MAX_PITCH_FOR_HEADING_DEG` | 60 | degrees | beyond this heading is "unreliable" (doc 40 suggestion; T-P4-01-015 measures it) | P4-01 | Provisional |
| `ARRIVE_RADIUS_M`, `ARRIVE_SPEED_MPS` | 0.6, 0.3 | m, m/s | plan P4-05 | P4-05 | Provisional |
| `MIN_HOLD_S` | 2 | s | an instruction changes at most this often unless the bearing changes by over 90 degrees | P4-05 | Provisional |
| `REPEAT_S` | 6 | s | plan P4-06 | P4-06 | Provisional |
| `OFF_ROUTE_M`, `OFF_ROUTE_S` | 1.5, 3 | m, s | plan P4-06 | P4-06 | Provisional |
| `DUP_WAYPOINT_M` | 0.7 | m | plan P4-04; must exceed `ARRIVE_RADIUS_M` | P4-04 | Provisional |
| `WAYPOINTS_MAX` | 200 | waypoints | a house, not a city; bounds route search | P4-03 | Provisional |
| `WAYPOINT_ALIASES_MAX` | 8 | aliases | each at most `WAYPOINT_NAME_MAX_CHARS` | P4-03 | Provisional |
| `WAYPOINT_DESCRIPTION_MAX_CHARS` | 1,000 | chars | | P4-03 | Provisional |
| `ROOM_NAME_MAX_CHARS` | 64 | chars | | P4-03 | Provisional |
| `CLAIM_STATEMENT_MAX` | 300 | chars | memory poisoning limit; also the SQLite `CHECK` | P4-08 | Provisional |
| `CLAIMS_PER_PERSON_MAX` | 1,000 | claims | store bound | P4-08 | Provisional |
| `MEMORY_BLOCK_MAX_TOKENS` | 2,000 | tokens | memory block in the prompt (plan gives no value) | P4-08 | Provisional |
| `NEXT_ASK_MAX_DAYS` | 90 | days | doc 40 suggestion | P4-08 | Provisional |

### A.8 Robustness, demo and robot (P5, G10, R cards)

| Name | Value | Unit | Why | Card | Status |
| --- | --- | --- | --- | --- | --- |
| `RLZ_TRACK_GAP_MS` | 500 | ms | tracking not ok for longer makes the frame suspect | P5-01 | Provisional |
| `RLZ_MARKER_MAX_AGE_MS` | 200 | ms | a sighting must be current | P5-01 | Provisional |
| `RLZ_EXTENT_TOL` | 0.2 | fraction | apparent marker size vs printed size | P5-01 | Provisional |
| `RLZ_SIGHTING_AGREE_M`, `RLZ_SIGHTING_AGREE_DEG` | 0.05, 2 | m, degrees | two sightings agree | P5-01 | Provisional |
| `RLZ_SIGHTING_MAX_HZ` | 10 | Hz | sighting event rate | P5-01 | Provisional |
| `RLZ_DRIFT_ACCEPT_M` | 0.3 | m | larger disagreement goes through SUSPECT | P5-01 | Provisional |
| `RLZ_MAX_TRANSLATION_M` | 20 | m | a transform cannot move the map across the house | P5-01 | Provisional |
| `RLZ_HOME_ZONE_M` | 3 | m | | P5-01 | Provisional |
| `RLZ_MAX_WALK_M`, `RLZ_MAX_AGE_S` | 30, 600 | m, s | LOCALIZED becomes STALE (T-P5-01-054 measures drift) | P5-01 | Provisional |
| `RLZ_MIN_BASELINE_M` | 2.0 | m | two-waypoint alignment | P5-01 | Provisional |
| `RLZ_LENGTH_TOL` | 0.10 | fraction | baseline lengths agree | P5-01 | Provisional |
| `RLZ_MAX_CHAIN` | 2 | transforms | | P5-01 | Provisional |
| `EVT_ABSENCE_S` | 120 | s | absence before a greeting | P5-02 | Provisional |
| `EVT_COOLDOWN_S` | 600 | s | per person (plan) | P5-02 | Provisional |
| `EVT_GLOBAL_GAP_S` | 60 | s | between unprompted utterances | P5-02 | Provisional |
| `EVT_UNPROMPTED_PER_HOUR_MAX`, `EVT_UNPROMPTED_PER_DAY_MAX` | 6, 40 | turns | hourly and daily caps (named here; review M6) | P5-02 | Provisional |
| `EVT_LEFT_AFTER_S` | 30 | s | out of view before "left" | P5-02 | Provisional |
| `EVT_MAX_DEFER_S` | 10 | s | a deferred event is dropped after | P5-02 | Provisional |
| `EVT_FACE_FLOOR_S` | 10 | s | face service runs at least this often | P5-02 | Provisional |
| `EVT_BODY_MAX_HZ` | 5 | Hz | per event name | P5-02 | Provisional |
| `EVT_QUEUE_MAX` | 256 | events | | P5-02 | Provisional |
| `PLR_EMBED_MS_MAX` | 200 | ms | keeps up with 1 Hz with margin | P5-03 | Provisional |
| `PLR_PRIOR_TTL_S` | 30 | s | | P5-03 | Provisional |
| `PLR_STEP_TOL` | 0.3 | fraction | step-distance tolerance | P5-03 | Provisional |
| `DEMO_START_S_MAX` | 60 | s | demo self-check | P5-04 | Provisional |
| `DEMO_MIN_BATTERY_PCT` | 50 | percent | | P5-04 | Provisional |
| `DEMO_PREEMPT_MS` | 300 | ms | speaker arbiter | P5-04 | Provisional |
| `GATE_MIN_UTTERANCES` | 50 | utterances | G10 latency median (5.2 rule 4; DEC-122 (DEC-45-04)) | G10 | Provisional |
| `GATE_PRICE_MAX_AGE_D` | 14 | days | R-01 kit prices | R-01 | Provisional |
| `ROBOT_SPEED_CAP_MPS` | 0.25 | m/s | first drives (doc 50 Q15, now DEC-030) | R-02 | Deferred |
| `SAFE_LEASE_PERIOD_MS` | 150 | ms | motion lease renewal | R-02 | Deferred |
| `SAFE_LEASE_TIMEOUT_MS` | 300 (never above 500) | ms | lease expiry stops motors | R-02 | Deferred |
| `SAFE_LOC_STOP_MS` | 500 | ms | stop after localization loss | R-02 | Deferred |
| `ROBOT_POSE_MAX_AGE_MS` | 200 | ms | | R-05 | Deferred |
| `ROBOT_STOP_CONFIRM_MS` | 500 | ms | `stop` must confirm zero velocity | R-05 | Deferred |

### A.9 Harness and gate thresholds (not runtime limits; in `tools/chaos/thresholds.py`)

| Name | Value | Unit | Why | Status |
| --- | --- | --- | --- | --- |
| `QUICK_SUITE_MAX_S` | 30 | s | pre-commit budget (section 9) | Provisional |
| `PRE_PUSH_MAX_S` | 120 | s | | Provisional |
| `TIMER_TOLERANCE_MS` | 20 | ms | real-timer tolerance on Windows (C10) | Provisional |
| `MUTATION_SCORE_MIN` | 0.80 | fraction | security modules, once DEC-102 (DEC-MUTATION-TOOL) is settled | Provisional |
| `SOAK_RSS_SLOPE_MAX_MB_PER_H` | 5 | MB/h | brain memory growth in soak | Provisional |
| `BODY_PSS_SLOPE_MAX_MB_PER_H` | 5 | MB/h | phone app memory growth (T-AND-012) | Provisional |
| `SOAK_TASKS_MAX_GROWTH` | 0 | tasks | asyncio tasks after warm-up | Provisional |
| `CORPUS_FILE_MAX_BYTES` | 10,485,760 | bytes | one corpus file (doc 10 section 9.2) | Provisional |

### A.10 Code versus spec differences found while building this table

1. **Token length.** The spec and `auth.py` use a 32-byte token, but `BrainServer.__init__` accepts any token of 16 bytes or more (`server.py:57`). Set it to `TOKEN_BYTES`.
2. **Body types are looser than the wire.** `AudioChunk` checks `sample_rate_hz >= 8000` with no upper bound and `channels >= 1` with no upper bound (`types.py:303-304`); the schema says 8,000 to 192,000 and 1 or 2. `Image` has no upper bound on width or height (`types.py:252-253`); the schema says 8,192. Wire data passes the schema first, so this matters for SimBody, ReplayBody and future bodies.
3. **Binary timestamps.** `unpack_binary` checks only `ts < 0` (`protocol.py:275`); the spec bound is 0 to 2^53 - 1 (`pack_binary` checks both, `protocol.py:258`). H-19.
4. **Spec-silent timeouts.** `BINARY_PAIR_TIMEOUT_S` (10 s) and `REQUEST_TIMEOUT_S` (10 s) are in code but not in the spec.
5. **Clock stability** is "held to 20 ms" in the spec, but the code only reports it; nothing fails or warns when it is exceeded.
6. **Kotlin names differ** from Python for the same limit (`MAX_DATA`, `DEFAULT_HEARTBEAT_MS`, `DEFAULT_LIVENESS_MS`, `HANDSHAKE_TIMEOUT_MS`, `MAX_ECHO_*`). `Limits.kt` uses the Appendix A names.
7. **One limit in two constants**: `LINK_RECHECK_S` (`phone.py:29`) and `PHONE_LINK_RECHECK_S` (`__main__.py:32`), both 60.
8. **Tool and wire `max_edge` differ** by design (1,568 vs 4,096), but the handoff from the tool to the command is untested.
9. **Documents that disagreed** and how this table decides: image bytes (doc 20 2 MiB vs doc 30 8 MiB: split into phone and file limits); utterance length (doc 30 30 s vs plan 15 s: 15 s); log line cap (doc 10 2,048 vs doc 50 1 KiB: 2,048); reply length (doc 20 1,500 vs catalogue 2,000: one cap of 2,000); tool result and tool JSON names (doc 10: renamed or merged as noted in A.4). The 13 Nov 2026 reconciliation (doc 50 section 7) then took the owning card's document value for `PEER_LOG_LINES_PER_MIN_MAX`, `LOG_FILES_MAX`, `UNSOLICITED_IMAGES_PER_MIN_MAX`, `XMP_PACKET_MAX_BYTES`, `LLR_ABS_MAX`, `MEMORY_BLOCK_MAX_TOKENS` and the P5 and robot rows changed above, kept this table's value where it had a stated reason (`MAX_IMAGE_BYTES`, `UTTERANCE_MAX_S`, `SPOKEN_TEXT_MAX_CHARS`, the archive caps) and took the safer value where a person could be harmed (`PLAY_PEAK_MAX_DBFS`, `BATTERY_TEMP_MAX_C`, `ROBOT_STOP_CONFIRM_MS`). The documents were renamed to these names.
10. **Names used by the documents but not yet in this appendix** (card-local thresholds, mostly in docs 20, 30, 40 and 45) are listed in doc 50 section 7.3. Each is Provisional and is added here by its owning card when the code lands; until then the owning document's table is the source.
