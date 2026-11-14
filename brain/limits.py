"""Named limits: the one home for every size, count, rate, time and tolerance bound in the brain (H-11).

docs/testing/00 section 3.1 and Appendix A are canonical for names and values. Product modules import
their limits from here (older module-level names such as `protocol.MAX_TEXT_BYTES` still exist as
re-exports, so callers keep working). Each constant carries its reason and, where it is not an
Appendix A "Existing" value, the word Provisional: the owning card confirms it by measurement or a
recorded decision.

Rules (tests/test_limits.py enforces them):
- every constant here is used by product code (brain/ or tools/migrate.py) and by at least one test,
  except the documented exceptions listed in that test;
- the limits the body app also enforces ("Kt" below) are pinned against protocol/v0.md and the body app's
  constants (`Limits.kt` does not exist yet: its names still differ, Appendix A.10 item 6; hardening batch 3).

Not limits (kept where they are): format constants fixed by the spec (the 20-byte binary header,
close codes), regular expressions, and algorithm tuning parameters (the clock estimator's horizon and
aging). Money caps are Jeff's choice and live in config.toml; this file holds only their sanity bounds.

This module imports nothing from the brain, so any module may import it.
"""

# ---- A.1 transport and protocol (P0-05) -------------------------------------------------------------------
MAX_TEXT_BYTES = 65_536  # one text frame, UTF-8 bytes (spec section 1). Kt. Existing.
MAX_BINARY_BYTES = 8 * 1024 * 1024  # data in one binary frame (spec section 5). Kt (BinaryFrame MAX_DATA). Existing.
BINARY_HEADER_BYTES = 20  # fixed by the spec (section 5); not a tunable limit, named so MAX_FRAME_BYTES is exact.
MAX_FRAME_BYTES = MAX_BINARY_BYTES + BINARY_HEADER_BYTES  # websockets max_size after authentication. Existing.
PRE_AUTH_MAX_BYTES = 16 * 1024  # largest frame before authentication (hello and auth are about 1 kB). Existing.
MAX_PENDING_HANDSHAKES = 4  # unauthenticated connections at once; a new one evicts the oldest. Existing.
FIRST_FRAME_TIMEOUT_S = 2.0  # a real body sends hello at once; idle sockets are dropped quickly. Existing.
HANDSHAKE_TIMEOUT_S = 10.0  # the whole handshake (spec section 4.2). Kt (HANDSHAKE_TIMEOUT_MS). Existing.
WS_RECV_QUEUE_FRAMES = 4  # websockets max_queue: frames buffered per connection before TCP backpressure, so at
#                           most 4 x MAX_FRAME_BYTES (32 MiB) before the text check. Reviewed for H-04: kept.
HEARTBEAT_MS = 2000  # spec section 7. Kt (DEFAULT_HEARTBEAT_MS). Existing.
LIVENESS_TIMEOUT_MS = 6000  # three missed heartbeats. Kt (DEFAULT_LIVENESS_MS). Existing.
HEARTBEAT_MS_MIN = 100  # hello.heartbeat_ms bounds in the schema (H-12). Appendix A.
HEARTBEAT_MS_MAX = 60_000
LIVENESS_TIMEOUT_MS_MIN = 300  # hello.liveness_timeout_ms bounds in the schema; must also exceed the heartbeat.
LIVENESS_TIMEOUT_MS_MAX = 300_000
SEND_TIMEOUT_S = 30.0  # a blocked send aborts the connection (spec section 7). Was a literal.
BINARY_PAIR_TIMEOUT_S = 10.0  # wait for the binary half of a message (not in the spec, A.10 item 4). Was a literal.
REQUEST_TIMEOUT_S = 10.0  # Link.request default (not in the spec, A.10 item 4). Was a literal.
CLOSE_TIMEOUT_S = 2.0  # close handshake wait. Was a literal.
CLOSE_REASON_MAX_CHARS = 120  # a WebSocket close reason fits 123 bytes (ASCII reasons only). Was a literal.
LINK_TIMEOUT_S_MAX = 3600.0  # largest handshake, binary or send timeout a Link or BrainServer is built with (H-12).
MAX_PEER_ERRORS_PER_MIN = 20  # more protocol errors from the peer in a minute is fatal (spec section 7). Kt.
MAX_ID = 2**32 - 1  # message ids (spec section 3). Kt. Existing.
MAX_TS = 2**53 - 1  # timestamps in microseconds (spec section 6). Kt. Existing.
ERROR_MESSAGE_MAX_CHARS = 500  # error.message (spec section 9; schema maxLength). Was a literal.
RECENT_IDS_KEPT = 1024  # peer ids remembered to refuse a repeat (H-25); an older replay is not detected.
TOKEN_BYTES = 32  # the pairing token (spec section 4.2). Kt (LinkAuth TOKEN_BYTES). Existing.
TOKEN_MIN_BYTES = TOKEN_BYTES  # the server and link refuse a shorter token (H-12).
NONCE_BYTES = 32  # handshake nonce (spec section 4.2). Existing.
ACL_TOOL_TIMEOUT_S = 15.0  # the icacls subprocess that makes the token folder private. Was a literal.
CLOCK_MAX_FIT_PPM = 200.0  # drift-fit sanity bound. Kt (ClockOffset MAX_FIT_PPM). Existing (renamed).
CLOCK_HISTORY_MAX = 5_000  # estimator history samples (2.8 h at one per 2 s), as on the body. Was a literal.
ECHO_TEST_MAX_COUNT = 10_000  # echoes in one probe (the phone enforces the same; H-18). Kt (MAX_ECHO_COUNT).
ECHO_TEST_MAX_BYTES = 1_048_576  # bytes in one echo, both sides (H-18); the brain refuses a larger echo (H-04).
ADB_LINK_RECHECK_S = 60  # re-run the reverse port forward (spec section 1). One constant now (was two, H-11).

# Handler queue and floods of valid frames (H-04, H-09; DEC-104). All Provisional.
HANDLER_QUEUE = 256  # messages waiting for the brain's handler; beyond this the body gets `busy`. Existing.
HANDLER_QUEUE_MAX_BYTES = 64 * 1024 * 1024  # bytes (text plus binary) waiting for the handler; `busy` beyond. The
#   count alone allowed 256 x 8 MiB = 2 GiB (H-04). Appendix A proposed 32 MiB; 64 MiB per hardening batch 2.
EVENT_MAX_BYTES = 16 * 1024  # one event message as text (events are names and small data). Provisional.
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # one still from the body (A.5; far below the wire's 8 MiB). Kt later. Provisional.
AUDIO_IN_MAX_CHUNK_BYTES = 5_120  # one microphone chunk: two nominal 80 ms chunks at 16 kHz mono (A.5). Provisional.
PEER_MSG_MAX_PER_S = 100  # valid messages per second per type from the peer (token bucket refill). Provisional.
PEER_MSG_BURST = 100  # bucket size: messages per type the peer may send at once. Provisional.
PEER_RATE_SANITY_MAX = 100_000.0  # largest rate or burst a BrainServer may be built with (tests raise the default).
PEER_HEARTBEAT_MAX_PER_S = 10  # heartbeat requests per second (a real peer sends one per 2 s). Provisional.
PEER_ECHO_MAX_PER_S = 2_000  # echo requests per second: above the about 600 a window-4 echo test reached on
#   loopback (measured 13 Nov 2026), so a real test never gets `busy`. Provisional.
PEER_EXCESS_MAX_PER_MIN = 1_000  # messages over their type's rate in one minute before the link is closed
#   (sustained flooding, DEC-104). Provisional.
PEER_LOG_LINES_PER_MIN_MAX = 60  # log lines a peer can cause per minute per connection (T-P0-05-047). Provisional.
LOG_RECORD_MAX_BYTES = 2048  # one log record's message, UTF-8 (T-P0-05-047, H-08). Provisional.
LOG_PEER_KEYS_SHOWN = 8  # how many keys or names of a peer's message one log line may list (H-08). Provisional.
LOG_PEER_ITEM_MAX_CHARS = 40  # each such name, truncated (H-08). Provisional.

# ---- A.2 body interface and tools (P0-06) -------------------------------------------------------------------
CAPTURE_MAX_EDGE_PX_MIN = 64  # capture_image.max_edge on the wire (spec section 8). Kt. Existing (was a tuple).
CAPTURE_MAX_EDGE_PX_MAX = 4096
POSE_RATE_MAX_HZ = 30.0  # set_pose_rate (spec section 8). Kt. Existing (renamed from MAX_POSE_RATE_HZ).
WIRE_IMAGE_EDGE_MAX_PX = 8192  # image width and height in the wire header (schema `image`); Image enforces it (H-34).
AUDIO_SAMPLE_RATE_HZ_MIN = 8_000  # PCM sample rate on the wire (schema `pcm`); AudioChunk enforces it (H-34).
AUDIO_SAMPLE_RATE_HZ_MAX = 192_000
AUDIO_CHANNELS_MAX = 2  # mono or stereo (schema `pcm.channels`) (H-34).
QUAT_NORM_TOL = 1e-3  # a unit quaternion's norm may differ from 1 by this much, then it is renormalised. Was a literal.
EVENT_QUEUE = 256  # body event stream, drop oldest. Existing.
POSE_QUEUE = 16  # body pose stream, drop oldest. Existing.
AUDIO_QUEUE = 64  # body audio stream, drop oldest. Existing.
POSE_WAIT_S = 2.0  # get_pose tool waits this long for a pose. Existing.
DEFAULT_MAX_EDGE_PX = 640  # capture_image tool default (the catalogue's default). Existing.
MAX_ARGS_BYTES = 64 * 1024  # tool arguments as JSON; also the per-string and per-name cap. Existing.
MAX_ARGS_DEPTH = 8  # tool argument nesting. Existing.
MAX_ARGS_NODES = 2000  # values in one tool call's arguments. Existing.
MAX_KEYS_SHOWN = 5  # argument names in a log line or a refusal detail. Existing.
LOG_KEY_MAX_CHARS = 40  # one shown argument name or path part. Was a literal.
TOOL_NAME_MAX_CHARS = 64  # a refused tool name as shown. Was a literal.
TOOL_DEFAULT_TIMEOUT_S = 15.0  # registry default timeout. Existing (renamed from DEFAULT_TIMEOUT_S).
TOOL_TIMEOUT_MAX_S = 600  # catalogue sanity bound on timeout_s. Was a literal.
ABANDONED_TASKS_PER_TOOL_MAX = 4  # handlers that ignore cancellation, per tool, before it is shut off (H-20).

# ---- A.3 config, CLI, logs and migration (P0-04) --------------------------------------------------------------
PORT_MIN = 1  # TCP port range for --port and [network] port (H-18, H-05). Well-known ports are not refused: the
PORT_MAX = 65535  # loopback bind needs no privilege on the machines in use and the spec names no floor.
BODY_CHECK_WAIT_MAX_S = 3600.0  # `body-check --wait`: an hour is longer than any real wait (X01-F02). Provisional.
CONFIG_MAX_BYTES = 65_536  # config.toml, checked before it is parsed (H-05). Provisional.
ENV_FILE_MAX_BYTES = 16_384  # .env, checked before it is parsed (H-05). Provisional.
ENV_VALUE_MAX_CHARS = 4_096  # one .env value (API keys are about 100 characters). Provisional.
ENV_KEY_MAX_CHARS = 64  # one .env key name. Provisional.
CONFIG_TEXT_MAX_CHARS = 253  # a config text value such as [network] host (a DNS name is at most 253). Provisional.
CONFIG_PATH_MAX_CHARS = 260  # a config path value (the classic Windows MAX_PATH). Provisional.
MODEL_NAME_MAX_CHARS = 128  # a [models] id. Provisional.
EGRESS_HOSTS_MAX = 64  # entries in [egress] allowed_hosts (a handful are expected). Provisional.
SPEND_CAP_SANITY_MAX = 100.0  # USD: largest money cap config.toml may set, so a typo cannot set a huge one (A.4).
LLM_REQUESTS_PER_HOUR_MAX = 3_600  # sanity bound on [caps] requests_per_hour (one a second). Provisional.
OUTPUT_TOKENS_SANITY_MAX = 8_192  # sanity bound on [caps] max_tokens (default 1,024, A.4 MAX_OUTPUT_TOKENS).
LOG_CAPTURE_MB_MAX = 1_024  # sanity bound on [logging] capture_max_mb (full-prompt capture, P0-07). Provisional.
LOG_CAPTURE_DAYS_MAX = 90  # sanity bound on [logging] capture_max_days (default REQUEST_LOG_RETENTION_DAYS).
REQUEST_LOG_RETENTION_DAYS = 14  # full-prompt capture kept this long by default (doc 50 B-10). Provisional.
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024  # one rotating brain log file. Was a literal.
LOG_FILE_BACKUPS = 3  # rotated brain log files kept. Was a literal.
RESOLVED_TTL_S = 600.0  # addresses learned for a listed egress host stay reachable this long. Existing.
SCRYPT_LOG_N = 17  # archive key derivation, 2**17 (about 128 MiB); restore accepts ONLY this (H-01). Existing.
PASSPHRASE_MIN_CHARS = 12  # archive passphrase. Existing (renamed from MIN_PASSPHRASE).
ARCHIVE_MAX_BYTES = 8 * 1024**3  # a migration archive larger than this is refused before it is read (H-01).
ARCHIVE_UNPACKED_MAX_BYTES = 32 * 1024**3  # bytes extracted from one archive: a gzip bomb stops here (H-01).
ARCHIVE_MEMBERS_MAX = 200_000  # entries in one archive (a huge member count is a bomb too). Provisional.
