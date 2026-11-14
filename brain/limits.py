"""Named limits (docs/testing/00 section 3.1 and Appendix A).

SEEDED BY CARD X-01 STAGE 1a, DELIBERATELY SMALL. This file holds only the limits the chaos harness
checks that have no home in the code yet. Moving the existing constants here (MAX_TEXT_BYTES,
MAX_ARGS_DEPTH and the rest) is remediation item H-11; until it lands they stay where Appendix A says
they are enforced today, and the harness imports them from there.

Both values below are Provisional (Appendix A.1). No product code enforces them yet; the X-01 log
monitor (tools/chaos/monitors.py) does, which is how the unbounded event log line (H-08) shows up.
"""

LOG_RECORD_MAX_BYTES = 2048  # one log record's message, UTF-8 (T-P0-05-047, H-08). Provisional.
PEER_LOG_LINES_PER_MIN_MAX = 60  # log lines a peer can cause per minute per connection (T-P0-05-047). Provisional.

# ---- hardening batch 1 (docs/testing/50 section 4.A). Values from Appendix A unless marked. ----------------------
PORT_MIN = 1  # TCP port range for --port (H-18). Well-known ports are not refused: the loopback bind needs no privilege
PORT_MAX = 65535  # on the machines in use and the spec names no floor. Provisional.
ECHO_TEST_MAX_COUNT = 10_000  # echoes in one probe (the phone enforces the same; H-18, T-P0-04-012). Appendix A.
ECHO_TEST_MAX_BYTES = 1_048_576  # bytes in one echo (H-18, T-P0-04-013). Appendix A.
BODY_CHECK_WAIT_MAX_S = 3600.0  # `body-check --wait`: an hour is longer than any real wait (X01-F02). Provisional.
TOKEN_MIN_BYTES = 32  # pairing token length the server and link must hold (H-12, protocol spec). Appendix A.
HEARTBEAT_MS_MIN = 100  # hello.heartbeat_ms bounds in the schema (H-12). Appendix A.
HEARTBEAT_MS_MAX = 60_000
LIVENESS_TIMEOUT_MS_MIN = 300  # hello.liveness_timeout_ms bounds in the schema; must also exceed the heartbeat. Appendix A.
LIVENESS_TIMEOUT_MS_MAX = 300_000
ABANDONED_TASKS_PER_TOOL_MAX = 4  # handlers that ignore cancellation, per tool, before the tool is shut off (H-20). Provisional.
LOG_PEER_KEYS_SHOWN = 8  # how many keys or names of a peer's message one log line may list (H-08). Provisional.
LOG_PEER_ITEM_MAX_CHARS = 40  # each such name, truncated (H-08). Provisional.
LINK_TIMEOUT_S_MAX = 3600.0  # largest handshake, binary or send timeout a Link or BrainServer is built with (H-12). Provisional.
