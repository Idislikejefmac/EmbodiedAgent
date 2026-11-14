"""Harness and gate thresholds (docs/testing/00 Appendix A.9). These are not runtime limits of the
brain: runtime limits live in brain/limits.py or where Appendix A says they are enforced today.
All values are Provisional until a gate confirms them."""

QUICK_SUITE_MAX_S = 30.0  # pre-commit budget for `chaos --quick`
PRE_PUSH_MAX_S = 120.0
TIMER_TOLERANCE_MS = 20  # real-timer tolerance on Windows (C10): a 1 ms asyncio sleep takes about 16 ms

CHAOS_CASE_TIMEOUT_QUICK_S = 10.0  # a case still running after this is recorded as `timeout`
CHAOS_CASE_TIMEOUT_FULL_S = 60.0
TRACE_MEMORY_QUICK = False  # tracemalloc roughly doubles the run time; full mode always traces memory
CASE_MEMORY_PEAK_MAX_BYTES = 256 * 1024 * 1024  # Python heap growth (tracemalloc peak) allowed in one case
CASE_LEAKED_TASKS_MAX = 16  # tasks still pending when a case's check returns
CASE_LOG_RECORDS_HARD_MAX = 10_000  # the log capture stops storing after this many (it still counts)

QUICK_SEEDS = (1,)
FULL_SEEDS = (1, 2, 3, 4, 5)
SEED_MAX = 2**63 - 1

RESULTS_KEEP = 50  # result files kept in data/chaos/
