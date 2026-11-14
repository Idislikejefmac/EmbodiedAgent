# Body interface, tool registry and egress guard: decisions (card P0-06)

Written 13 Nov 2026; revised the same day after the second-opinion review (section 6 lists what changed). This records what was decided and why, what the next cards rely on, and what is still open. The how-to for a new body is `docs/adding_a_body.md`.

## 1. Body interface

| Decision | Why |
| --- | --- |
| The interface is **async**; streams are `BodyStream` objects the caller closes, from any task | The transport is asyncio and every call can wait on a remote device. Closing a stream is how a body learns to switch a sensor off, and that request usually comes from another task |
| Streams are bounded queues fed by the adapter, never Python generators. **Drop policy for every stream: drop the oldest, count it in `dropped`**; audio loss also shows as a `seq` gap. Sizes: events 256, poses 16, microphone chunks 64 | A generator cannot be closed while a consumer waits on it, and it hands a stalled consumer a stale backlog. Fresh sensor data is worth more than stale |
| **Request/response** for one-off things (`status`, `capture_image`, `navigate`, `stop`, `speak_text`, `play_audio` per chunk); **streams** for continuous ones (`events`, `pose_stream`, `audio_in_stream`) | Matches the protocol; `play_audio` per chunk gives backpressure and lets barge-in cut between chunks |
| **Mandatory core** (abstract): lifecycle, `clock_sync`, `status`, `events`, `stop`. **Gated methods are concrete** in `Body` and refuse by default | The agent must always be able to ask about its body and to stop it. A capability added later (lidar scan, odometry) must not force an edit to every existing body |
| **`status()` never raises.** Unreachable: `connected=False`. Connected but not answering (timeout, busy, refusal): `connected=True, stale=True`, readings None | One stable contract for the P1-05 watchdog and for the agent: "my body is not answering" is an answer |
| **Capabilities**: six names (`pose`, `image`, `audio_in`, `audio_out`, `speak_text`, `navigate`), one per gated method. Present means the method works; absent means it raises `NotSupported` at the call and does nothing. `NotYetImplemented` is a subclass. Capabilities are dynamic: empty while the body is unreachable | One rule for callers whether the body will never have the feature or does not have it yet. The contract tests enforce it both ways |
| `navigate` takes a pose goal in a named frame, not a waypoint name; a carried body never has it | Waypoints are the brain's knowledge. The `navigate_to` tool decides between driving and the guidance loop |
| `stop(motion, audio)` is one method with two switches | Barge-in silences speech without cancelling a drive |
| `speak_text` (body's own text-to-speech) is a capability | Card P0-07 needs it for the G1 skeleton (`speak_local`). It may go at G2 |
| **Time**: every object carries `t_us` on the brain's monotonic clock plus the raw `t_body_us`. Adapters convert with the transport's existing estimator; `Body.clock_sync()` exposes offset, RTT and drift | Nothing above the body does clock arithmetic. SimBody runs its clock 76,222 s ahead on purpose |
| **Frames and units** follow `protocol/v0.md` section 4.4 exactly; units are in field names | One convention from wire to waypoint |
| **`Image.camera_pose` is the pose of the camera that took the picture**, not of the body | On a phone the two coincide; on a robot they do not, and everything that places what a picture shows (waypoint photos, P1-02, P4) needs the camera. The wire field `image.pose` is now defined the same way (`protocol/v0.md` 4.6) |
| `Image.encoding` may be `jpeg` or `png` | The wire carries only JPEG; SimBody makes PNGs with the standard library |
| Only an adapter may raise `link_state`; a body's event of that name is dropped, and consumers check `source` | A body must not be able to announce a false link state |
| `LinkBody` (protocol v0, any device) is separate from `PhoneBody` (adds only the adb port forward) | ReplayBody (P1-03) can reuse LinkBody's decoding; a second protocol device needs no new adapter |
| `BrainServer` gained `on_connect` / `on_disconnect` callbacks | The adapter needs to know when its body arrives and leaves. No protocol change |
| `config.toml [body] kind` defaults to `sim` (Jeff, 13 Nov) | No listening socket and no adb unless asked. G1 and `serve` name the phone explicitly |

Deferred to the card that owns the device side: pose, stills, microphone, speaker and `speak_local` over the link. Note for P1-03: ReplayBody must re-base recorded `t_us` onto the replaying process's clock (see `brain/body/replay.py`).

## 2. Tool schemas and the registry (what P0-07 builds on)

- The allowlist is `brain/agent/tools.v0.json`. **Listed** (input and output schemas): `get_pose`, `capture_image`, `speak`, `listen`, `who_is_present`, `navigate_to`, `stop`, `get_body_status`. **Reserved** (name, class, channels and sensitive flag fixed now; schema written by the owning card): `where_am_i`, `route_to`, `describe_place`, `list_waypoints`, `recall`, `save_waypoint`, `remember`, `set_quiet`, `start_enrollment` (sensitive), `delete_person_data` (sensitive).
- **Stop, quiet and mute are three things** (Jeff, 13 Nov). `stop` is a one-shot interrupt. Quiet mode is its own persistent tool, `set_quiet` (on or off), reserved here and built at P5-02; the app channel may call it because it only reduces activity. Microphone mute is a button in the app, not an agent tool.
- **Times shown to the agent are ages and durations in milliseconds**, never clock values (Jeff, 13 Nov): `get_pose.age_ms`, `capture_image.age_ms`, `listen.ended_ms_ago` and `listen.duration_ms`.
- Every tool has `class`, `channels` (`app`, `console`) and `sensitive`, and may set `timeout_s` and `timeout_plus_arg` (`listen` waits its `timeout_s` argument plus 10 s; the default is 15 s).
- Schema rules, checked when the catalogue loads: input schemas may use only a fixed list of keywords (`type`, `properties`, `required`, `additionalProperties`, `enum`, the numeric and length bounds, `pattern`, `items`, `default`, `description`, `x-kind`); `anyOf`, `oneOf`, `$ref`, `patternProperties`, `prefixItems` and anything else are refused, so no string can hide from the rules. Every object is closed; every string declares `x-kind` (`id`, `name`, `spoken_text`, `reason_text`) and `maxLength`; ids use the identifier pattern; names use a pattern with no path characters or `%`; no parameter may be named like a path, file, command, URL or host. Integers are strict.
- Beyond the schema, the registry refuses arguments that are not plain JSON (exact `dict`, `list`, `str`, numbers, booleans, null; no subclasses), nested deeper than 8, with more than 2,000 values or larger than 64 kB; strings with control characters (new line and tab are allowed in `spoken_text` and `reason_text`); and ids or names that are not NFKC-normalised or contain invisible format characters (such as the right-to-left override) or `%`. **P4-03 inherits this for waypoint names**: store and compare names in the same normalised form.
- `capture_image.max_edge` is 64 to 1568, default 640. 1568 is the longest edge worth sending for cost; the body interface itself still accepts up to 4096 for brain-side uses. `reason` stays required (Jeff, 13 Nov).
- `ToolRegistry.call(name, args, caller) -> ToolResult` does not raise, whatever it is given. Order of checks: on the allowlist; channel permitted; sensitive rule; built (a reserved or handler-less tool answers `not_built` only after the first three, so an unidentified request for `delete_person_data` is refused, logged and counted); arguments. Then the handler runs under the tool's timeout. At the timeout the registry answers `timeout` and discards whatever the handler returns later. **Handlers must not block the event loop**; no timeout can interrupt a blocking call.
- `ToolResult`: `status` is `ok`, `refused`, `unavailable` or `error`; `code` is one word (`unknown_tool`, `channel_not_permitted`, `identity_not_confirmed`, `invalid_arguments`, `not_built`, `body_not_supported`, `body_unavailable`, `busy`, `timeout`, `body_error`, `internal`); `message` is a fixed sentence the model can say; `detail` is a short fact the registry composed itself (the argument at fault, at most five unexpected names and a count, or the capability's name); `content` is the tool's JSON; `attachments` are `Image` objects. **No text from a handler, an adapter or an exception ever reaches the model.** A body that has not built a method yet gives `not_built`, not `body_not_supported`.
- Refusals are counted (`registry.refusals`) and logged at WARNING on `brain.agent.tools` as `REFUSED tool=... channel=... band=... code=... arg_keys=[...]`: at most five argument names and a count, never values.
- `registry.model_tools(channel)` gives `[{name, description, input_schema}]` for tools that have a handler and that the channel may call, with the `x-` annotations removed. For P0-07: map a `ToolResult` to a tool result with the JSON text of `to_json()`, each attachment as an image block (`image.media_type`, base64 of `image.data`), and the error flag set when `ok` is false. Pass a tool call through to the registry even when its arguments look wrong; the registry refuses it.
- `brain/agent/body_tools.py` gives handlers to `get_body_status`, `get_pose`, `capture_image`, `speak` and `stop` for any `Body`.

### Who is calling: the channel rule (P0-07 must follow this)

The registry trusts the `Caller` it is given, so the agent loop must build it correctly, and **never from anything the model says**:

1. `Caller.for_turn(sources, band=..., evidence=..., person_id=...)` builds it. `sources` lists where every piece of content in the model's context for this turn came from (`InputSource`): `local_console`, `remote_console`, `body`, `tool_result`.
2. The turn's channel is that of the **least privileged** content (`turn_channel`): `CONSOLE` only if everything in the context was typed at the brain's own stdin on the laptop. One spoken sentence, picture, button event or tool result makes the turn `APP`. Text in a photo can steer the model, so a call made after the model has seen such content cannot be credited to the console.
3. Only the local stdin console is `CONSOLE`. The networked "direct connection over WiFi" is `remote_console` and stays `APP` until its authentication is hardened.
4. The console is exempt from the sensitive-tool identity rule only in that sense (Jeff, 13 Nov). In practice: do privileged console work in a context that holds no app content.
5. `band`, `evidence` (`voice`, `face`) and `person_id` come from the identity layer (P3-10), per turn. `person_id` lets `start_enrollment` require a confirmed Jeff and `recall` check whose notes are asked for.

## 3. Egress chokepoint

`brain/egress.py`. `config.toml [egress] allowed_hosts` is the list; entries are exact host names or IP addresses, optionally `host:port` (443 otherwise); no wildcards; a missing list denies everything.

Two layers:

1. **Import lint** (`tests/test_egress.py`): only `brain/egress.py` may import a network client library or a vendor SDK (`socket`, `ssl`, `http`, `urllib.request`, `httpx`, `httpx2`, `requests`, `aiohttp`, `anthropic`, `_overlapped`, `winreg`, speech vendors and so on). `brain/transport/` and `brain/body/link.py` may import `websockets` for the loopback link. `subprocess` and `ctypes` are allowed only in `transport/auth.py` and `body/phone.py`. Calls such as `open_connection`, `sock_connect`, `os.system`, the `os.exec*` and `os.spawn*` families, `posix_spawn`, `exec`, `eval`, `compile` and `import_module` are flagged anywhere else.
2. **Process guard** (`install_guard`, called by `python -m brain` before anything else, and for the whole test suite): an audit hook refuses any name lookup, reverse lookup or connection that is not allowed, before anything is sent. On Windows asyncio connects through overlapped I/O, which raises no audit event, so the guard also wraps the event loop's `connect` and `sendto`. It is **one-shot**: a second call with a different policy raises. It **tests itself** at install (a blocking and an asyncio connection to the documentation address 192.0.2.1 must both be refused) and refuses to start the brain otherwise, so a change in asyncio's internals fails loudly instead of opening a gap.

What is allowed:

- a listed host on its port. Addresses are reachable only if a lookup of a listed name returned them in the last 10 minutes, and only on that name's ports;
- **loopback only to a port this process itself listens on** (the body link; the event loop's socket pair), or a port named in the policy's `local_ports` (`python -m brain probe` names the port it dials). The first version allowed all loopback, and the review showed the hole: with `HTTP_PROXY=http://127.0.0.1:<port>` a request for an outside address was delivered in full to a local listener, because the guard saw only a loopback connection. Any local proxy or forwarder would have done the same.
- For the same reason `install_guard` **removes the proxy environment variables** (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` and the like, any case) and logs their names. Windows registry proxy settings, which `urllib` reads, are not changed; a client that follows them is stopped at its connection to the proxy, local or remote.

Consequence: `python -m brain probe --host <another machine>` is refused unless that host and port are in `[egress]`.

**The Anthropic client (P0-07, P1-04).** The earlier claim here, that the guard alone confines the SDK "whatever base URL, proxy setting or redirect it is given", was wrong for a loopback proxy and has been withdrawn. What holds now: the SDK's HTTP client opens ordinary Python sockets, so under the guard it can reach `api.anthropic.com:443`, cannot reach any other outside host, and cannot reach a local proxy. That was checked with plain sockets and `urllib`, not with the SDK, which is not installed. P0-07 builds the client in one factory in `brain/egress.py` (the only file allowed to import the SDK), which calls `policy.check("api.anthropic.com")`, and passes the SDK an HTTP client built with environment proxy settings switched off (`trust_env=False`) and an explicit transport; check the SDK's current documentation for the class to pass as `http_client` (the 1.x SDK is built on `httpx2`). If the agent loop needs the SDK's exception types, re-export them from `brain/egress.py`. P1-04's budget guard wraps the same factory. The SDK retries connection errors, so a refused destination shows as a connection error after its retries, with `EGRESS DENIED` in the log.

### P0-07 checklist: verify the claim with the real SDK

Do not skip; record the results in the P0-07 report. For both the sync and the async client:

- [ ] The client is constructed only in `brain/egress.py`, with `trust_env=False` and an explicit transport; the import lint still passes.
- [ ] Under the guard, with an audit hook recording `socket.getaddrinfo` and `socket.connect` (and the wrapped event-loop `connect`), one real API call succeeds and the only destination recorded is `api.anthropic.com` port 443.
- [ ] `base_url` set to a documentation address (`https://192.0.2.1/`) fails with `EgressDenied` in the cause chain and nothing is sent.
- [ ] `base_url` on port 80 (`http://api.anthropic.com/`) fails the same way.
- [ ] With an empty `[egress]` list the call fails the same way.
- [ ] With `HTTPS_PROXY` and `ALL_PROXY` pointing at a loopback listener in another process, set before start: the variables are gone after `install_guard`, the call still goes direct, and the listener receives nothing. Repeat with the proxy passed to the client explicitly: refused, listener receives nothing.
- [ ] Streaming requests behave the same as non-streaming ones.

Limits, stated plainly:

- The guard sees Python's socket module only. A C extension with its own networking, direct calls into `_overlapped` or `ctypes`, or a child process (adb is one, and is the approved link to the phone) bypass it. The import lint keeps such code out of the brain.
- There is no outbound firewall rule underneath, by decision (Jeff, 13 Nov): a firewall filters addresses, not host names, so it cannot express this list.
- `socket.connect(("name", port))` with an unlisted name is refused, but the name may be looked up first; clients that resolve with `getaddrinfo`, which is all the usual ones, are stopped before the lookup.
- An address learned from a listed name stays reachable for 10 minutes after the lookup, even if the name has since moved.
- When a host has several addresses asyncio reports the refusals as one `OSError` whose text contains each refusal.

## 4. Type checking

`python -m mypy` runs mypy in strict mode over `brain/body`, `brain/agent` and `brain/egress.py` (`[tool.mypy]` in `pyproject.toml`); `tests/test_typing.py` runs it with the suite. `brain/transport`, `brain/config.py` and `brain/__main__.py` are imported but not yet checked strictly; bringing them in is a small separate job.

## 5. Decisions on the open questions (Jeff, 13 Nov 2026)

| Question | Decision |
| --- | --- |
| Default body | Stays `sim`. G1 and `serve` pass the phone explicitly |
| Quiet or mute | Three things: `stop` (one-shot), `set_quiet` (persistent tool, reserved, P5-02), microphone mute (app button, not a tool) |
| Console and sensitive tools | Exempt from the identity rule only for the local stdin console; see the channel rule |
| Timestamps shown to the agent | Ages and durations, not clock values |
| Outbound firewall rule | No: it cannot filter by host name |
| `capture_image.reason` | Stays required |
| Type checker | mypy, strict |
| Which reserved tools are sensitive | Still open beyond `start_enrollment` and `delete_person_data`: reading another person's notes through `recall` depends on the arguments, so P3-10 needs a per-person rule (`Caller.person_id` is there for it) |

## 6. Changed after the second-opinion review

Registry: cannot raise (argument checks are bounded and wrapped); unexpected argument names capped at five plus a count; a handler cannot outlast or override its timeout; per-tool timeouts; `NotYetImplemented` maps to `not_built` and no adapter text reaches the model; boundary checks run before `not_built`; attachments are type-checked; input schemas limited to listed keywords; names and ids normalised and free of invisible characters and `%`; `Caller` gained `person_id`, validates its evidence, and is built per turn by `Caller.for_turn`. Body: streams are closable from any task with one drop policy; gated methods are concrete defaults; `status()` never raises (`stale`); `Image.camera_pose`; bodies cannot send `link_state`. Egress: loopback limited to own ports; proxy variables removed; reverse lookups covered; one-shot install with a self-test; learned addresses expire; wider import lint. Tools: `listen` returns ages; `capture_image.max_edge` capped at 1568; `set_quiet` reserved. mypy strict added.

## 7. Pending

- **Real-phone status check (card step 4).** The phone was out of range. To run it: stop `serve`, then `python -m brain body-check --body phone --wait 60` and press Connect in the app. Pass is `tool get_body_status: ok/ok` with `connected: True`, `stale: False` and the phone's real `battery_pct` and `charging`. Everything else about PhoneBody is tested against the protocol's test body on loopback.
- The checklist in section 3, in P0-07.
