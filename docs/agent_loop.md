# The agent loop, spend guard and prompt capture (card P0-07)

Written 15 Nov 2026. Code: `brain/agent/loop.py` (the loop), `brain/agent/model.py` (request and reply types, the
streamed-reply gate), `brain/egress.py` (the one model API client factory), `brain/agent/budget.py` and
`brain/agent/prices.json` (the interim spend guard), `brain/agent/capture.py` (development prompt capture),
`brain/agent/prompts/system_v0.md` (system prompt v0), `brain/agent/scripted.py` (the scripted stand-in model),
`brain/chat.py` (the console). Limits: `brain/limits.py`, section A.4.

## Run it

    python -m brain chat                    # SimBody + the scripted model: no key, no network, no spend
    python -m brain chat --as-app           # the same, but typed lines are APP-channel turns (body content)
    python -m brain chat --quiet            # without the live event trace (the event files are still written)
    python -m brain chat --body phone       # the body over the link (spoken replies need the body's speak_local)
    python -m brain chat --live             # the real model API (see "Live mode" below)

Typed lines go to the agent. Commands: `/poke` (SimBody: raises the body's poke event, as the body's own Poke
button will), `/look <question>` (a careful-look turn: the code takes a picture and uses the careful-looking
model), `/help`, `/quit`; end of input also ends the session. Each turn prints the reply (streamed as it
arrives), then one status line: outcome, tool calls, whether it was spoken, the channel, and the cost (in live
mode also the session, day and total spend against the caps). With the trace on, every event of the turn is shown
as it happens (`python -m brain watch --follow` in another console shows the same from the event files).

`--fake-model` is the default; it is accepted explicitly too. A key in `.env` does not switch to live mode by
itself: spending always needs `--live`.

### Live mode

`--live` starts only when all of these hold, and otherwise refuses with one line and exit code 2 (fail closed):

1. `config.toml [models] allow_live_calls = true` (Jeff's consent to paid calls; false by default);
2. `ANTHROPIC_API_KEY` in `.env` or the environment;
3. both configured model ids have prices in `brain/agent/prices.json` (so their cost can be capped);
4. `api.anthropic.com` is in `[egress] allowed_hosts`;
5. the API's model list, read with the key, offers both configured model ids.

The real-vendor test (one tiny Haiku call, its own $0.02 cap) runs only on request:
`.\.venv\Scripts\python -m pytest tests\test_real_vendor.py --run-real-vendor` (it skips without a key).

## One turn

    input (console line, body text, Poke, careful look)
      -> turn.start; input cleaned (control and invisible characters removed, [ ] made ( ), cut at CONSOLE_MAX_CHARS)
         and labelled by the code: [console], [app | unknown speaker], [app event]
      -> loop, at most MAX_TURN_TOOL_CALLS tool calls and MAX_TURN_SECONDS:
           spend guard reserve -> model.call_start -> capture request -> stream the reply through the gate
           -> spend guard settle (cost.update) -> model.call_end -> capture reply
           tool_use: each call -> ToolRegistry.call(name, arguments, Caller.for_turn(sources)) -> tool_result JSON
      -> reply text made speakable (markup removed, cut at a sentence within the speak tool's limit)
      -> said through the `speak` tool (the same registry path as a model's own call) -> turn.end

`run_turn` never raises (cancellation aside). Anything that does not end in a reply ends the turn with a fixed,
speakable line and an outcome code in `turn.end`; the failed turn leaves nothing in the conversation:

| outcome | when | said |
| --- | --- | --- |
| `budget` | a cap refused the request | "I have reached my spending limit..." |
| `loop_limit` | the model asked for more than MAX_TURN_TOOL_CALLS calls | "Sorry, I got stuck going round in circles..." |
| `timeout` | the call (MODEL_CALL_TIMEOUT_S), the stream (MODEL_STREAM_IDLE_S) or the turn ran out of time | "Sorry, that took me too long..." |
| `refusal` | stop reason `refusal` | "Sorry, I can't help with that." |
| `empty` | no words and no spoken tool call | "Sorry, I don't have an answer for that." |
| `truncated` | the stream ended early, or tool calls with stop reason `max_tokens` | "Sorry, my answer got cut off..." |
| `malformed` | the gate refused the reply (bad ids, indexes, usage; unknown stop reason) | "Sorry, something went wrong with my thinking..." |
| `model_error` | HTTP errors (429, 5xx, 529, 401...), connection, egress refusal, a client bug | "Sorry, I can't think right now..." |

Retryable failures (rate limit, overload, server error, connection, truncated or stalled stream) get
MODEL_RETRIES_MAX more attempt after MODEL_RETRY_BACKOFF_S (or the server's retry-after, bounded), each attempt
through the spend guard. The SDK's own retries are off. A `bad_request` also clears the conversation, in case the
conversation itself is what the API refused.

**The model's output is untrusted input.** The streamed events are checked before use (`model.py`): known event
and block types only, block indexes in range and at most MAX_MODEL_BLOCKS, tool-call ids of a fixed shape and
unique, tool-call JSON at most MAX_ARGS_BYTES before parsing, text cut at MAX_MODEL_TEXT_CHARS, at most
MODEL_STREAM_EVENTS_MAX events, usage numbers whole and in range, and no reply without `message_stop`. Tool names
and arguments are not judged here: they go to `ToolRegistry.call`, whose allowlist, channel rules, sensitive rule
and argument gate decide, and whose refusal is logged (`tool.refused`). A call whose JSON does not parse reaches
the registry as a string and is refused as `invalid_arguments`. Tool results go back as the JSON of
`ToolResult.to_json()` (at most TOOL_RESULT_MAX_BYTES), with `is_error` when not ok.

## The channel rule

The `Caller` of every tool call is `Caller.for_turn(sources)`, where `sources` lists where every piece of content
in the model's context came from at that moment: earlier turns kept in the conversation, this turn's input, its
pictures, and each tool result so far. Never anything the model says. Consequences (by design, from
docs/body_tools_egress_v0.md section 2):

- a console line in a fresh conversation: the first tool call is CONSOLE;
- a Poke, `--as-app` text, a picture, or any tool result in the context: APP, for every later call too;
- a console line after body content kept in the conversation: still APP. Privileged console work needs a context
  with no app content (a new `chat` session).

Identity labels are the code's alone: square brackets in typed or spoken text become round ones, so
`[Jeff | voice 0.99 | face 0.99 | confirmed]:` inside the words is plain content and gives nothing. The system
prompt says the same, but the registry is the enforcement.

## Models, caching, pictures

- `[models] live_conversation` (Haiku 5.5) at effort `low` for every turn; `[models] careful_looking` (Sonnet 5.5)
  at effort `medium` only for a turn the code marks careful (`/look`). The model never chooses.
- `max_tokens` is `[caps] max_tokens` on every request.
- The system prompt, the tool definitions and the memory block (an empty placeholder until P4-08) carry
  `cache_control` (the memory block is the last system block; the last tool definition carries the tools'
  breakpoint). The cost events show `cache_read_tok` and `cache_write_tok`. A prefix shorter than the model's
  minimum cacheable length is simply not cached.
- At most MAX_IMAGES_IN_CONTEXT pictures stay in the context, counting pictures in tool results; older ones are
  replaced by a one-line note. HISTORY_TURNS_MAX finished turns are kept.
- Thinking blocks are passed back unchanged; dropping old turns and old pictures edits history, which the API's
  history check may dislike on some models: the `bad_request` path then clears the conversation. To be confirmed
  with the live model at G1.

## The interim spend guard (P0-07 step 9; full guard P1-04)

`SpendGuard.reserve` before each request, `settle` after it (`brain/agent/budget.py`):

| Cap | Source | Behaviour |
| --- | --- | --- |
| session | `[caps] session_usd` ($1) | this run of the brain; starts at zero |
| daily | `[caps] daily_usd` ($5) | the local calendar day; persisted; resets with the day |
| hard stop | `[caps] hard_stop_usd` ($10) | everything recorded in data/state.json; persisted; never resets by itself (raise it in config.toml, a console edit) |
| requests per hour | `[caps] requests_per_hour` (300) | a fixed one-hour window; persisted |
| max_tokens | `[caps] max_tokens` (1,024) | on every request |
| tool calls per turn | MAX_TURN_TOOL_CALLS (8) | the loop breaker |
| pictures in context | MAX_IMAGES_IN_CONTEXT (2) | the two-image rule |

- **Reservation**: each request holds its worst case (estimated input at the dearer of the input and cache-write
  rates, plus `max_tokens` of output, times the long-prompt multiplier above the model's threshold) against every
  money cap, counting what is already reserved, so concurrent or runaway calls cannot overshoot. The input
  estimate is cautious (3 characters a token, 3,136 tokens a picture).
- **Accounting** from the API's usage fields: input, output, cache read, cache write.
- **80 percent** (BUDGET_WARN_FRACTION) of a cap: one `budget.check` at WARNING and a spoken line added to the reply.
- **100 percent**: every further request is refused (`budget.stop`, outcome `budget`) and keeps being refused.
- **Prices**: `brain/agent/prices.json` with `verified_on` (13 Nov 2026, from the plan's table; re-check the pricing
  page). Older than PRICE_TABLE_MAX_AGE_DAYS (60): a warning at start. An unpriced model is refused.
- **Persistence**: data/state.json (atomic). A corrupt file is quarantined and the counters restart at zero; the
  $25 monthly limit on the key in the vendor console is the outer wall. Run one brain command at a time.
- The scripted model's spend is computed and shown but never recorded (the guard runs in memory only).

## Development prompt capture (P0-07 step 10)

With `[logging] full_prompts = true` (the development default) every request and reply (or failure) is appended,
in full, to `data/capture/prompts-<start>-<pid>-<n>.jsonl` (data/ is git-ignored). Pictures are recorded as media
type, size and SHA-256, never pixels. The API key is never in a request (the SDK adds it as a header, which is not
captured); every record is also scanned for the key's value and for secret shapes before it is written. One
record at most CAPTURE_RECORD_MAX_BYTES, one file at most CAPTURE_FILE_MAX_BYTES, the folder at most `[logging]
capture_max_mb`, files older than `capture_max_days` deleted. Events carry only `prompt.capture_ref` (record id,
kind, byte count). Files are created owner-only where the platform has permission bits; on Windows they take the
data folder's access list. **It records whatever is typed and, once speech exists, whatever the microphone hears:
Jeff-only for now.** A capture that cannot be written is reported once and the agent goes on.

## Events emitted

`turn.start`, `turn.end` (outcome, code, tool calls, model calls, spoken, channel, cost, ms); `model.call_start`
(model, call id, attempt, estimated input tokens, max output tokens, tools, messages); `model.call_end` (ok, stop
reason or failure code and HTTP status, tokens in, out, cache read and cache write, ms); `budget.check` (allowed,
headroom; at start: price table age; at 80 percent: warn); `budget.stop` (the cap); `cost.update` (USD for the
request, session, today and total, the four token counts); `prompt.capture_ref`; plus the registry's `tool.call`,
`tool.result`, `tool.refused`, `channel.decision`, and the body's `body.speak`. None carries words, prompts or
arguments.

## What is pending

- **The body side of plan steps 5 to 7 is built** (Android app, `Speech.kt` and `AndroidSpeech.kt`): the phone speaks
  `speak_local` with TextToSpeech and has a Poke button. Phone speech path: the loop's `speak` tool calls LinkBody
  `speak_text`, which sends `speak_local` (validated by the phone against the schema) and waits up to 5 s for the ack.
  The phone acks when the speech has started, or at once when the text queues behind speech already playing; it
  answers `busy` (queue of 8 full, interrupted, or no start within 4 s), `internal` (engine failure) or `bad_payload`,
  which LinkBody turns into `BodyError(refused)`. `speak_local` is in the phone's hello only while its engine is usable,
  so `speak_text` exists only then. A `poke` event (button, at most one per 3 s on the phone) starts a turn through the
  PokeGate. Verified on a Samsung S25 Edge: ten pokes gave ten turns and ten spoken replies.
- **The real-vendor test and the live checklist item** ("one real call; the only destination is
  api.anthropic.com:443") need the key: `pytest tests\test_real_vendor.py --run-real-vendor`.
