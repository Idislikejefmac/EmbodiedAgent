# Observability: live trace, health panel and crash barrier (card X-02)

The brain can always show what it is doing, in real time, even when the camera, the microphone or the
phone link are down; and no input can crash it. This page says how to watch it, what the events and the
health states mean, how to add an event for a new feature, what is redacted, and what the crash barrier
can and cannot promise. Code: `brain/obs/`. Limits: `brain/limits.py` (the `OBS_`, `SUPERVISOR_`,
`LAUNCHER_`, `STATE_` and `CRASH_` names).

## Watch it

    python -m brain serve                      # writes data/logs/events-*.jsonl, data/health.json, data/state.json
    python -m brain watch --follow             # the live trace (another console)
    python -m brain watch --health             # the health panel, refreshed every second
    python -m brain health                     # the health panel once (says STALE if the brain stopped writing it)
    python -m brain demo-trace                 # a scripted conversation against SimBody: no hardware needed
    python -m brain demo-trace --unavailable   # the same with the body link, camera and microphone unavailable
    python -m brain serve --supervised         # restart the brain if it dies unexpectedly (see the crash barrier)

`watch` filters: `--component agent` (repeatable), `--level WARNING` (minimum), `--type 'tool.*'` (glob,
repeatable), `--session s-...`, `--turn t-3`, `--since 10m` (or `2h`, `1d`, or `2026-11-13T10:00`).
`--detail` shows every field. Without `--follow` it replays the saved files, so any session can be read
after the fact; `--source <folder or events-*.jsonl file>` reads files copied elsewhere.

One compact line per event:

    05:38:56.589 WARNING agent      tool.refused  [s-20261113-...|t-3] run_command refused: unknown_tool  tool=run_command code=unknown_tool channel=app

The viewer reads the event files and `health.json` only (a test checks every file it opens), opens no
socket, and cleans every string again before printing, so a damaged or hostile file cannot write control
characters to the terminal. (The card also allowed a loopback channel to a running brain; reading the files
gives the same live view with no listener to secure, so none was built.)

## The event schema

One JSON line per event (`brain/obs/events.py`):

| key | meaning |
| --- | --- |
| `v` | schema version (1) |
| `seq` | event number in this process, from 1 |
| `ts`, `mono` | wall time (UTC, for people) and `time.monotonic()` (for order and durations; it does not jump) |
| `level` | DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `comp` | component: transport, body, agent, config, egress, obs, supervisor, launcher, brain, demo, model, speech, identity |
| `type` | the decision point (the catalogue below) |
| `session`, `turn` | correlation ids: a body connection's session id; an agent turn id (from P0-07) |
| `msg` | a short sentence |
| `f` | bounded, redacted fields |

### The catalogue

Every type is listed in `EVENT_TYPES` with its component, default level and meaning; the chaos manifest
lists them too (`event_type.*`), so adding or removing one is reviewed.

- Transport: `link.connect` (debug), `link.auth`, `link.up`, `link.down`, `link.replaced`, `link.evicted`,
  `link.refused`, `link.peer_error`, `link.unconsumed`, `link.session_summary`, `link.forward`,
  `watchdog.fired`, `flood.rate_limited`, `flood.closed`, `flood.too_large`, `queue.busy`, `clock.estimate`,
  `clock.reset`.
- Body: `body.start`, `body.close`, `body.link`, `body.capabilities`, `body.status`, `body.event`,
  `body.frame` (camera still: id, size, never pixels), `body.audio` (every 25th microphone chunk),
  `body.stream`, `body.speak` (character count, never the words), `body.stop`, `body.navigate`,
  `body.not_supported`, `body.request`, `queue.drop`, `validation.failed`.
- Agent: `tool.call` (names, types and sizes of the arguments, never values), `tool.result`, `tool.refused`,
  `tool.timeout`, `tool.shut_off`, `tool.failed`, `channel.decision`, `sensitivity.decision`, `turn.start`,
  `turn.end` (demo-trace today; the agent loop from P0-07).
- Process and config: `config.loaded`, `config.error`, `startup.self_test`, `startup.refused`,
  `egress.denied`, `brain.start`, `brain.stop`, `process.crash`, `probe.result`, `pair.result`.
- The pipeline itself: `obs.started`, `obs.sink_failed`, `obs.sink_recovered`, `obs.dropped`,
  `obs.bad_event`, `health.change`, `state.loaded`, `state.quarantined`, `state.save_failed`.
- Crash barrier: `supervisor.started`, `supervisor.crashed`, `supervisor.restart`, `supervisor.gave_up`,
  `supervisor.fatal`, `supervisor.stopped`, `launcher.start`, `launcher.exit`, `launcher.restart`,
  `launcher.gave_up`.
- Demo: `demo.step`.
- **Reserved for later cards** (named and documented, so the card only calls `emit`): `model.call_start`,
  `model.call_end`, `cost.update`, `budget.check`, `budget.stop` (P0-07 step 9, the interim spend guard),
  `prompt.capture_ref` (P0-07 step 10: the record id and byte count of an entry in the PRIVATE capture file;
  full prompts never enter the event stream), `speech.stt`, `speech.tts` (P2), `identity.decision` (P3-10).
  The health panel already maps `model.call_end`, `speech.*` and `budget.*` to their sources.

Types marked `crash` (`supervisor.crashed`, `tool.failed`, `process.crash`) report an exception nothing
expected, caught by an isolation point; the chaos harness counts one as a crash.

## Add an event for a new feature

1. Add the type to `_ALL` in `brain/obs/events.py` (name `area.what`, component, default level, a sentence).
   If it replaces a reserved name, change its status to live.
2. Call it at the decision point: `from brain import obs` then
   `obs.emit("area.what", "short sentence", key=value, ...)`. Use plain values: names, codes, counts, sizes,
   durations, ids. Never put a secret, a template, a transcript, a prompt or an argument value in it.
   A string a body or a model chose may go in a field (it is cleaned); to show it in the message, wrap it
   with `peer(...)` from `brain.obs.redact` (%r, cut).
3. If the feature adds an entry point, map it in `ENTRY_COVERAGE` (or say why not in `ENTRY_EXEMPT`):
   `tests/test_obs_events.py` fails for an unmapped tool, message, Body method or CLI command.
4. `python -m brain chaos --manifest-stub`: add the new `event_type.*` line to `tools/chaos/manifest.toml`.
5. If it says something about a health source, add it to `_HANDLERS` in `brain/obs/health.py`.

`emit` never raises and never blocks (about 20 us per event on the development laptop with the file writer
running; the budget is `OBS_EMIT_BUDGET_US`, 50 us, checked by a test).

## Redaction (at the source)

Every field goes through `brain/obs/redact.py` before the event exists:

- a field whose name contains a secret or content word (token, key, secret, password, mac, nonce, auth,
  cookie, credential, embedding, template, prompt, transcript, text, content, value, args, arguments,
  payload, pixels, pcm, biometric, faceprint, voiceprint) is `<redacted>`;
- any string shaped like an API key, a 64-hex token, a bearer token, a `NAME_KEY=...` line or a URL with a
  password is `<redacted:secret>`, whatever field it is in;
- control characters, Unicode line separators, bidirectional overrides and lone surrogates are written as
  `\xNN` / `\uNNNN`, so an event can never forge a line;
- strings are cut to `OBS_FIELD_MAX_CHARS`, lists to `OBS_LIST_ITEMS_MAX`, fields to `OBS_FIELDS_MAX`, nesting
  to `OBS_NEST_DEPTH_MAX`; byte strings show only their length; a whole event is at most `OBS_EVENT_MAX_BYTES`;
- tool arguments appear only as `name:type:size`.

Canary tests (`tests/test_obs_events.py`) and the chaos harness (`X-02.obs.emit`, and every case of every
experiment through `structured_sink`) prove that no canary secret reaches an event, the ring or a file.

## The pipeline (it can never harm the brain)

`emit` -> redaction -> per-type limit (`OBS_TYPE_BURST`, then `OBS_TYPE_RATE_PER_S`; crash and health-change
events are never held back) -> ring buffer (`OBS_RING_EVENTS`) -> in-process sinks (the health board; the
chaos monitor) -> a bounded queue (`OBS_WRITER_QUEUE_MAX`, beyond it dropped and counted) -> the writer
thread -> `data/logs/events-<start>-<pid>-<n>.jsonl`. A file is never renamed: at `OBS_FILE_MAX_BYTES` the
writer starts the next, and keeps the newest `OBS_FILES_MAX`. A file that cannot be written (disk full,
locked, read-only folder, permission denied) is reported ONCE (`obs.sink_failed`, health `log_sink` down),
events are dropped and counted meanwhile, and it is retried every `OBS_SINK_RETRY_S`; the brain's behaviour
does not change (a test runs the same session with a working and a broken event folder).

## The health panel

`data/health.json` is rewritten atomically about every `OBS_HEALTH_INTERVAL_S`. Each source has a state, a
reason and the age of the last thing heard from it:

| state | meaning |
| --- | --- |
| up | working, recently heard from |
| degraded | working with a problem (restarting, dropping, stale reading, low battery, little disk) |
| down | not working, and the reason says why (a missing camera is visible, not silent) |
| unknown | not built yet, or nothing heard yet, with the reason |

Sources: `body_link`, `camera`, `microphone`, `speaker` (from the body's capability set and from frames,
audio and speech actually flowing), `clock`, `battery`, `temperature`, `model_vendor`, `speech_vendors`
(unknown until P0-07 and P2), `queues` (event, handler and stream drops), `budget` (placeholder until P0-07
step 9; the counters live in `data/state.json`), `disk` (free space under data/), `log_sink`, `config`,
`supervisor` (the supervised components). A snapshot older than `OBS_HEALTH_STALE_S` is shown as STALE.

## The crash barrier

- **Components** (`brain/obs/supervisor.py`). `serve` runs the transport server, the phone-link keeper, the
  health writer and the event-writer watchdog as supervised components. An unhandled exception is caught,
  reported (`supervisor.crashed`: type and `file:line`, never values; the traceback goes to the text log
  file only), and the component restarts after a backoff doubling from `SUPERVISOR_BACKOFF_INITIAL_S`
  (0.5 s) to `SUPERVISOR_BACKOFF_MAX_S` (30 s). After `SUPERVISOR_RESTARTS_MAX` (5) failures within
  `SUPERVISOR_WINDOW_S` (60 s) it is stopped and reported (`supervisor.gave_up`). If that component is the
  transport server, `serve` ends with exit code 1 (so `--supervised` restarts the whole process).
- **Security refusals stay fatal.** A component that raises `FatalComponentError` (for example the frame
  limit before authentication cannot be enforced) is never retried and never falls back to a weaker mode;
  `serve` ends with exit code 2.
- **Shutdown always works.** Cancellation is never swallowed; `stop()` cancels everything and waits at most
  `SUPERVISOR_STOP_TIMEOUT_S` per run, reporting (not hanging on) a component that ignores cancellation.
- **Connections, background tasks and streams.** Each body connection runs behind its own barrier (a
  failure drops that connection, reported with its session id; the server goes on). The link's heartbeat,
  watchdog and dispatcher tasks are watched (`watch_task`). A body stream producer that raises is
  restarted with the same bounds; the consumer sees a gap, not an end.
- **The event writer thread** is restarted if it dies, within the same bounds.
- **main()** catches anything unexpected at the outermost level: one line on stderr, a crash report in
  `data/crash/` (exception type, a redacted message, `file:line` frames, the last events), exit code 1,
  never a raw traceback. A config, token-store or self-test refusal ends with its own clear message and
  exit code 2 (fail closed: that is a refusal, not a crash).
- **The process launcher** (`serve --supervised`, `brain/obs/launcher.py`): a parent process restarts the
  brain if it dies unexpectedly, records the exit code and meaning (`launcher.exit` and a crash file),
  backs off from `LAUNCHER_BACKOFF_INITIAL_S` to `LAUNCHER_BACKOFF_MAX_S`, and gives up after
  `LAUNCHER_RESTARTS_MAX` deaths in `LAUNCHER_WINDOW_S`. It never restarts after a clean stop (0) or a
  fail-closed refusal (2). Ctrl+C stops both.
- **State across a restart**: `data/state.json` (session id, start count, request and spend counters as
  placeholders for P0-07's guard, pending request ids) is written atomically (temporary file, then replace)
  and checked on load; a corrupt, oversize or wrong file is quarantined (`state.corrupt-*.json`, at most
  `STATE_QUARANTINE_KEPT`) and replaced by defaults, never a crash.

### Honest limits

No software can survive an operating-system kill (Task Manager, `taskkill /F`), a power cut, an
out-of-memory kill of the whole process, or a defect in the Python interpreter or a C extension (a crash
inside native code ends the process without any Python handler running). For those: the launcher restarts
the brain (if the launcher itself was not killed), the state file is consistent because every write is
atomic (the last complete write survives), and the event files keep everything up to the last flushed
line (a line being written at the instant of death may be cut; the viewer skips it). The per-type limit
can hold back ordinary events during a flood (counted, and reported as `obs.dropped`); crash events are
never held back. A thread blocked inside a C call cannot be interrupted by any supervisor. An exception
message is written to the text log with its traceback; it is redacted in events and crash reports, but the
text log is only as clean as the code's own messages (S9).
