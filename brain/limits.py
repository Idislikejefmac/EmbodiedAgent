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
