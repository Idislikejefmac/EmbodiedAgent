# On-phone re-check of the P0-05 link and the P0-06 PhoneBody status (15 Nov 2026)

Device: Samsung Galaxy S25 Edge (SM-S937U), Android 17, over wireless debugging. Build: debug APK, versionName 0.1.0, built from commit `f6d9a53`'s Kotlin sources (installed 15 Nov 2026, 11:00). Brain: `python -m brain serve` at commit `f6d9a53` (config gate, flood limits, handler byte budget, supervisor and live trace all in place). The earlier checks (13 Nov) were on the build before hardening batches 1 and 2; this run re-checks everything on the final code.

| Check | Result |
| --- | --- |
| Install and launch | `adb install -r` Success; app is the foreground activity |
| Unpaired body is refused | First connection got `unauthorized`, close 1008, as designed (fail closed) |
| Pairing | `python -m brain pair` delivered the token over adb stdin; value never printed |
| Authenticated connect | `body authenticated`; clock offset logged (best RTT 28.7 ms) |
| Echo, 1,000 messages x 100 kB | sent 1000, received 1000, lost 0, corrupt 0, dropped 0, id gaps 0, 76.6 s; both sides agree (brain `echo_test_result` and phone `ECHO RESULT`) |
| Hard kill of the brain, restart, no touch on the phone | Reconnected and authenticated 3.1 s after the new brain started (limit 10 s) |
| Explicit Stop | Service records 0 afterwards; the brain saw a clean session end |
| Give-up path (30 s unreachable limit, brain killed) | The phone stopped itself after about 34 s; service records 0 |
| Leftovers on the phone | No foreground service, no held wake lock of ours (history shows `REL EmbodiedBody:link`); the one held wake lock is the system's |
| Leftovers on the laptop | No brain or test processes; nothing listening on 8765 |
| PhoneBody status (card P0-06 step 4) | `get_body_status: ok/ok`, connected true, stale false, battery 76.0 percent, charging false; `stop` tool ok. Temperature and link RTT are not reported by the phone yet (null): the P1-05 watchdog adds them |

Notes: the echo took 76.6 s against 50 s on 13 Nov. The new per-type rate limits did not drop or refuse anything (no `busy`, `too_large` or rate-limited counters in the session summaries); the difference is consistent with the `adb reverse` delay ramp recorded in the protocol spec, and is worth re-measuring when the link carries audio (G3 and G4).

Not re-measured here: the clock-stability metrics (the brain's per-minute clock lines and `max_change_60s_ms` summary need a longer idle session) and the pending decision on the clock-stability definition.