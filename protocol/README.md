# Protocol

The body-to-brain WebSocket protocol, version 0 (card P0-05):

- `v0.md`: the written spec (transport, envelope, message types, authentication, binary frames, clocks, lifecycle and backpressure, command allowlist, errors, open questions, decisions).
- `v0.schema.json`: JSON Schema (2020-12) for every JSON message. `tests/test_protocol_schema.py` validates every example in `v0.md` against it and checks that the Kotlin body's constants agree.

Implementations: `brain/transport/` (Python: `protocol.py`, `auth.py`, `clock.py`, `link.py`, `server.py`, and the test body `client.py`) and the Kotlin body in `body-android/app/src/main/java/com/jmd/embodied/body/` (`Protocol.kt`, `BinaryFrame.kt`, `LinkAuth.kt`, `ClockOffset.kt`, `SendQueue.kt`, `BrainLink.kt`, `LinkService.kt`).
