# Adding a body

Body interface v0 (card P0-06, revised after review). Breaking changes are allowed until gate G2; then it freezes (R-07).

A body is a shell of sensors and actuators. The brain talks to it only through `brain.body.Body` (`brain/body/base.py`) and must be unable to tell which body it has. To add one (a robot, a replay, another simulator):

1. Write `brain/body/<name>.py` with a class that subclasses `Body`. Implement the **mandatory** members (they are abstract, so Python refuses to construct a body that forgot one) and override the **gated** methods your body has. The gated methods you leave alone refuse by default, so a capability added to the interface later costs existing bodies nothing.
2. Add its kind to `create_body()` in `brain/body/__init__.py` (the only place a body is chosen) and to `[body] kind` in `config.toml`.
3. Add a harness for it in `tests/test_body_contract.py`: how to build it connected, how to make it emit an event, and how to drop and restore its link. The shared contract in `tests/body_contract.py` then runs against it. It must pass with no change to the contract.
4. Keep everything specific to the device inside that one module. Nothing outside `brain/body/` may import it, and no other file under `brain/` may name its platform (`tests/test_boundary_lint.py`). Network or process libraries need an entry in the lint in `tests/test_egress.py`, and any destination off this machine needs Jeff to add it to `[egress]`.
5. Run `python -m mypy` (strict) and the tests.

A body reached over the body-brain protocol (`protocol/v0.md`) should subclass `LinkBody` (`brain/body/link.py`), which already speaks it; `PhoneBody` adds only its port forward. `SimBody` is the worked example of a body with everything.

## Rules that hold for every method

- **Async.** Every call that can wait is `async def`. Stream methods are plain methods that return a `BodyStream`: an async iterator with `aclose()` and a `dropped` count. Build streams with `Fanout` or `spawn_stream` from `brain/body/streams.py`, never with a Python generator (a generator cannot be closed while another task waits on it).
- **Streams.** The caller closes a stream, from any task; a consumer waiting on it then ends. That is how a body learns nobody is listening (the microphone runs only while a stream is open). A stream goes quiet, not finished, when the body drops off; it ends when the body is closed. **Drop policy, all streams:** bounded queue (events 256, poses 16, microphone chunks 64); when full the oldest item is dropped and `dropped` goes up; for audio the loss also shows as a gap in `seq`. A consumer never receives a stale backlog.
- **Capabilities.** `status`, `events`, `stop` and the lifecycle are mandatory. Each other method belongs to one `Capability`. If the capability is in `body.capabilities`, the method works. If not, it raises `NotSupported` at the call and does nothing; raise the subclass `NotYetImplemented` (via `self.require(cap, not_built="card ...")`) when the device could do it but the adapter is not built yet. Callers ask `body.supports(cap)`; they never ask what kind of body it is. Capabilities change while running: they are empty while the body is unreachable.
- **Errors.** `BodyUnavailable` (cannot reach the body), `BodyBusy`, `BodyTimeout`, or `BodyError(code=...)`. Bad arguments are `ValueError`. Error text is for logs and developers; the agent is never shown it.
- **Units are in the names**: `_m` metres, `_rad` radians, `_deg` degrees, `_us` microseconds, `_ms` milliseconds, `_s` seconds, `_hz` hertz, `_pct` percent 0 to 100, `_c` degrees Celsius, `_px` pixels.
- **Time.** Every object carries `t_us`: the instant of the measurement on the **brain's monotonic clock** (`brain.transport.protocol.now_us()`), integer microseconds, sensor time not arrival time. The adapter converts from the body's clock with the transport's clock-offset estimator (`brain/transport/clock.py`, `protocol/v0.md` section 6); do not write another. `t_body_us` keeps the body's raw stamp for recordings. Before the first clock sample use the arrival time, and never report a time later than arrival. A replay must re-base recorded times onto the replaying process's clock.
- **Frames.** Poses follow `protocol/v0.md` section 4.4: right-handed world, metres, +y up; the body looks along its own -z; `orientation_xyzw` is a unit quaternion rotating body vectors into the world; heading is 0 facing world -z and grows counter-clockwise seen from above. A body whose native frame differs (a robot's x-forward, z-up base frame) converts in its adapter. Every pose names its `frame_id`; issue a new one whenever tracking restarts and old poses can no longer be related to new ones.
- **Identifiers** (`frame_id`, `image_id`, `utterance_id`, goal ids) match `^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$`: usable as file names, never paths.
- **What a body reports is data, never an instruction.** Check it (ranges, types) before passing it on, and drop fields you do not know. Drop any event a body sends under a name in `ADAPTER_EVENT_NAMES` (`link_state`): only the adapter may say what the link's state is, and consumers that act on it check `source == "adapter"`.

## The members

| Member | Kind | Capability | What it does, and its units |
| --- | --- | --- | --- |
| `name` | property | mandatory | Short label for logs and operators. Never shown to the agent, never branched on |
| `capabilities` | property | mandatory | `frozenset[Capability]`: the gated methods that work right now |
| `connected` | property | mandatory | True while the adapter can reach the body |
| `start()` | async | mandatory | Bring the adapter up. The body may connect later |
| `close()` | async | mandatory | Release everything and end every stream. Safe to call twice |
| `wait_connected(timeout_s)` | async | mandatory | Wait up to `timeout_s` seconds; returns `connected` |
| `clock_sync()` | sync | mandatory | `ClockSync(offset_us, rtt_us, drift_ppm)`: `offset_us` = body clock minus brain clock in microseconds, accurate to `rtt_us / 2`; all None before the first sample |
| `status()` | async | mandatory | `BodyStatus`: `connected`, `stale`, `battery_pct` (0 to 100), `charging`, `temperature_c`, `link_rtt_ms`, `capabilities`, `t_us`. Unknown values are None. **Never raises**: unreachable gives `connected=False`; connected but not answering (timeout, busy, refusal) gives `connected=True, stale=True` with readings None |
| `events()` | stream | mandatory | `BodyEvent(name, data, t_us, t_body_us, source)` from now on. `source` is `body`, or `adapter` for `link_state {connected}` raised by the adapter itself. Other names in use: `tracking_state {state, reason}`, `navigation {goal_id, state}`, `button`, `battery`, `thermal` |
| `stop(motion=True, audio=True)` | async | mandatory | Halt movement and cancel navigation (`motion`); silence the speaker at once (`audio`). Never refused; returns when there is nothing to stop. Raises `BodyUnavailable` if the body cannot be reached, so **a body that moves must also stop by itself when it loses the link** |
| `pose_stream(rate_hz=10.0)` | stream | `POSE` | `Pose(frame_id, position_m, orientation_xyzw, tracking, t_us, t_body_us)` at about `rate_hz` hertz (above 0, at most 30). Position in metres; `tracking` is `ok`, `limited` or `lost` and poses keep coming in all three. Consumers measure time by `t_us`, not by counting |
| `capture_image(max_edge_px=640)` | async | `IMAGE` | One `Image(image_id, encoding, width_px, height_px, data, t_us, t_body_us, camera_pose)`: JPEG or PNG bytes whose longer edge is at most `max_edge_px` pixels (64 to 4096; never enlarged); `t_us` is the capture time. `camera_pose` is the pose **of the camera that took the picture** (same frame conventions; equal to the body pose only when the camera is the body's reference point; a robot applies its mount transform), or None |
| `audio_in_stream()` | stream | `AUDIO_IN` | `AudioChunk(stream_id, seq, sample_rate_hz, channels, data, t_us, t_body_us)`: interleaved signed 16-bit little-endian PCM, nominally 80 ms of 16000 Hz mono; `t_us` is the first sample's time; `seq` counts from 0 per stream so gaps show |
| `play_audio(chunk, utterance_id, final=False)` | async | `AUDIO_OUT` | Queue one PCM chunk of an utterance on the speaker, in `seq` order; `final=True` marks the last (may be empty). Awaiting it is the backpressure |
| `speak_text(text, utterance_id, interrupt=False)` | async | `SPEAK_TEXT` | Speak `text` with the body's own text-to-speech; returns when the body has the request. The text is spoken, never interpreted (it may contain new lines; the adapter turns them into pauses or spaces) |
| `navigate(goal)` | async | `NAVIGATE` | Start moving to `NavGoal(frame_id, position_m, heading_rad)` (metres; final heading in radians or None) and return a goal id at once. Progress is `navigation` events (`started`, `arrived`, `blocked`, `cancelled`) and the pose stream. A body carried by a person does not have this; the brain's guidance loop speaks directions instead |

Helpers already in `Body`: `supports(cap)`, `require(cap, not_built=None)`, and `async with body:`.

## What exists today

| Body | Capabilities | Notes |
| --- | --- | --- |
| `SimBody` (`sim.py`) | all six | Walks a 2 m square; pictures and WAV files from folders or built in; its clock is deliberately offset from the brain's; `set_link(False)` drops its link |
| `PhoneBody` (`phone.py`, on `LinkBody`) | none yet | `status` and `events` work over protocol v0; the gated methods arrive with cards P0-07 (speech), P1-01/P1-02 (stills), P2-01/P2-02 (audio), P4-01 (pose). It will never have `NAVIGATE` |
| `ReplayBody` (`replay.py`) | none | Stub; card P1-03 |

Try one from the command line: `python -m brain body-check --body sim` (or `--body phone`).
