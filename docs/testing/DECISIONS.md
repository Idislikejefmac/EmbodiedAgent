# Decisions waiting for Jeff

Status: merged register, 13 Nov 2026. Owner: Jeff. **Scope (Jeff, 13 Nov 2026):** near-term work is the phone as eyes, ears and mouth and the laptop (or other hosting hardware) as the brain; guidance of a person carrying the phone (P4-06, G9, `navigate_to` in carried mode) is phone-path work. A robot is far off: nothing is bought until face and voice recognition, history tracking and conversation over built knowledge are robust, so every robot-only decision is parked in the last section. Every entry is a **recommendation awaiting your answer**; nothing here is decided. Each one blocks the card or gate named in its "Blocks" column until you answer. This file merges every decision ID the test documents created (`DEC-HEADING` and the other named ones in doc 00, `DEC-10-nn`, `DEC-20-nn`, `DEC-ID-nn`, `DEC-SPA-nn`, `DEC-MEM-nn`, `DEC-45-nn`) and the open questions and plan-text decisions of the earlier doc 50 (`Q1` to `Q21`, `B-nn`). The old IDs stay valid as aliases: the documents now write `DEC-080 (DEC-HEADING)`. Doc 50 section 5 carries the same register.

How to answer: write "DEC-nnn: yes" (accept the recommendation) or your own answer, with the date, in this file or in the plan. A session then applies it to the plan and the test documents.

## Top 10 decisions to make first

These unblock the next work (the hardening pass, X-01 stage 1, P0-07) or close a window that gets harder later.

| # | ID | In one line | Recommendation | Why now |
| --- | --- | --- | --- | --- |
| 1 | DEC-021 | When does the laptop go back, and where will the project live after? | Pick a date; move to your own machine before any face or voice data exists | Every later card makes the move harder (keys, biometrics) |
| 2 | DEC-020 | May family face or voice data ever exist on this company laptop? | No. Until the move, the system is Jeff-only | Your employer can technically read this laptop; this decides where Phase 3 runs |
| 3 | DEC-068 | What spend cap applies before the full budget guard (P1-04) exists? | 60 model requests an hour, small per-session and per-day money caps, and a spend limit in the vendor console | P0-07 is the first card that spends money |
| 4 | DEC-025 | Should the request log keep full prompts (your words, transcripts)? | No full prompts until P2-04 sets a rule; log sizes, ids and costs only. If you want them, keep 14 days | P0-07 writes this log on its first run |
| 5 | DEC-106 | A typo or unknown key in `config.toml`: stop or warn? | Stop at start | A misspelt cap name would silently remove the cap; needed by the config hardening before P0-07 |
| 6 | DEC-100 | Accept X-01 (the test harness) in two stages? | Yes: stage 1 now; later parts attach to the cards they need | X-01 cannot be finished as written: parts need cards that do not exist yet |
| 7 | DEC-101 | Install the development test tools (Hypothesis, psutil, Bandit, pip-audit) with pinned hashes? | Yes, in X-01 stage 1; numpy and Pillow at P1-01 | Property, fuzz, security and dependency tests need them; a missing tool must fail, not skip |
| 8 | DEC-102 | Which mutation-testing tool, given mutmut may not run on Windows? | A small scripted mutator first; try mutmut once | Gate step 0 needs a working tool before any mutation score means anything |
| 9 | DEC-081 | What form must a map frame id take? | A fresh random UUID every time tracking starts or resets | The hardening pass fixes the SimBody frame-id bug (R-FND-57, score 20) and needs the form |
| 10 | DEC-104 | What happens when the phone floods the brain with valid messages? | Per-type rate limit; extra heartbeats and echoes answered `busy`; sustained flooding closes the link | The protocol hardening tests (T-P0-05-040) cannot be written without a policy |

Next after these: DEC-023 (what may be recorded before the consent gate; blocks P1-03), DEC-022 (how the encryption key is protected; blocks the first recording), DEC-080 (heading direction; blocks Phase 4), DEC-105, DEC-107, DEC-108, DEC-109 (small Phase 0 choices).

## How the register is organised

- **Merged ID** `DEC-001` to `DEC-124`, grouped by theme with gaps for growth: 001 to 019 identity and consent; 020 to 029 privacy and the company laptop; 030 to 039 robot (parked, last section); 040 to 049 behaviour and guidance safety on the phone path; 050 to 079 voice, latency and cost; 080 to 099 spatial and memory; 100 to 119 test infrastructure and the Phase 0 platform; 120 to 129 plan text and gates.
- **Blocks**: **NEXT** means it blocks the next work (the hardening pass of doc 50 section 4.A batches 1 and 2, X-01 stage 1, or P0-07). **NOW** means no card waits but the answer gets harder later. **Later** names the first card that waits. **Parked** means a robot card that waits for the buy decision (R-01, C11); those ten entries are only in the last section.
- **Aliases** are the IDs the documents used first; doc 50 `Qnn` are the earlier open questions.
- Where two documents recommended different answers, the entry says so and gives the pick with a reason (also listed in "Where documents disagreed" at the end).

## Summary

| ID | Question (short) | Recommended | Blocks | Level |
| --- | --- | --- | --- | --- |
| DEC-001 | Who records consent, and how | Console only, person present, own "yes" | P3-01 | Later |
| DEC-002 | Syd's age and guardian consent | Guardian consents too if under 18; Syd can still refuse | P3-01 (Syd) | Later |
| DEC-003 | Day 31 of a staged deletion | Purge for good at next start; ask if the clock jumped | P3-01 | Later |
| DEC-004 | Anyone may pause their own recognition | Yes, pause without proof; delete stays at the console | P3-01, P3-11 | Later |
| DEC-005 | Who may act on another person's data from the phone | Owner or the data subject only; enrollment owner only | P3-10 | Later |
| DEC-006 | Spoken random-word challenge for sensitive phone requests | Yes | P3-10 | Later |
| DEC-007 | The plan's "explicit second factor" | Delete it; the console is the fallback | P3-10 | Later |
| DEC-008 | Build a flat-face (photo) detector | Not for v1 unless an irreversible phone action is added | P3-04 (conditional) | Later |
| DEC-009 | Unknown voice during conversation mode | Ask once, then ignore unless spoken to | P3-08 | Later |
| DEC-010 | Keep enrollment audio and photos | Templates only | P3-03, P3-06 | Later |
| DEC-011 | Number of photo attacks at G5 | 3 per type per person | G5 | Later |
| DEC-012 | Stranger tests: volunteers or public data | Public licensed data only | P3-04, P3-07 | Later |
| DEC-013 | Third-party faces in digiKam photos and recordings | Read only consenting people's regions; keep nothing else | P3-03 | Later |
| DEC-014 | How often the agent uses names | Report at G7; guide about one reply in four | P3-11 | Later |
| DEC-015 | Where you live (biometric law) | Record it in `docs/privacy.md` before enrolling anyone | P3-01 | Later |
| DEC-016 | Decode images in a separate process | Not for v1; revisit if fuzzing finds a crash | P3-02, P1-01 | Later |
| DEC-017 | How the registry is encrypted | In-memory SQLite saved as one encrypted file | P3-01 | Later |
| DEC-018 | Loading voice model files safely | Convert once to ONNX or safetensors; never load pickle | P3-05 | Later |
| DEC-020 | Family biometric data on the company laptop | No; Jeff-only until moved | P3-01, Phase 2 with others | NOW |
| DEC-021 | Laptop return date and the move | Set a date; move before biometric data exists | none (process) | NOW |
| DEC-022 | How the encryption key is protected | Passphrase-derived key for anything biometric; DPAPI only for Jeff-only data | P1-03 first recording, P3-01 | Later |
| DEC-023 | What may be recorded before the consent gate | Recorder off by default; Jeff-only, encrypted, 7-day expiry | P1-03, P2-02, P2-04, P2-07 | Later |
| DEC-024 | Conversation audio to a cloud speech vendor | Yes, push-to-talk only until the wake word works | P2-04 | Later |
| DEC-025 | Full prompts in the request log | Not until P2-04 sets a rule; else 14 days | P0-07 | NEXT |
| DEC-040 | Who may turn quiet mode off | Console or a confirmed household member; never a TV, guest or event | P5-02 | Later |
| DEC-041 | Guidance when position may have drifted | Continue with a warning; refuse after twice the walk budget | P5-01 | Later |
| DEC-050 | Forced cut at 15 s | Contiguous continuation, flagged | P2-03 | Later |
| DEC-051 | End-of-speech silence and turn start | Keep 500 ms; turn starts at last speech sample | P2-03, P2-06 | Later |
| DEC-052 | What the flush and cancel numbers measure | Separate phone-side, brain-side and acoustic figures | P2-01, P2-05, P2-07 | Later |
| DEC-053 | How G4 interrupts a reply | A console key; PTT barge-in verified at P2-07 | P2-05, G4 | Later |
| DEC-054 | Image policies (extra, unsolicited, rotation, size) | `busy`; drop and count; rotate pixels; never upscale | P1-01 | Later |
| DEC-055 | Spend overshoot with parallel calls | Reserve worst-case cost before sending | P1-04 | Later |
| DEC-056 | Speech vendors get their own caps | Yes | P1-04, P2-04, P2-05 | Later |
| DEC-057 | Phone loses the link mid-sentence | Drain at most 2 s, then silence | P2-01 | Later |
| DEC-058 | Cancel during a tool call | Finish, discard, mark interrupted; never touch motion | P2-05 | Later |
| DEC-059 | Two replies at once | Newest wins | P2-05 | Later |
| DEC-060 | Speaking numbers and units | A sanitiser table you review | P2-05 | Later |
| DEC-061 | Wake phrase in the transcript | Strip it | P2-07 | Later |
| DEC-062 | Conversation-mode window renewal | Your turns renew it, up to a maximum | P2-07 | Later |
| DEC-063 | May voice widen listening | No; voice can only narrow | P2-07 | Later |
| DEC-064 | Own-voice guard on short utterances | Drop only exact matches | P2-07 | Later |
| DEC-065 | Showing the last-turn time on the phone | Drop the step; phone reports its own number | P2-06 | Later |
| DEC-066 | Phone temperature limit and where it is enforced | 40 C, enforced on the phone | P1-05 | Later |
| DEC-067 | How "stop" is heard during playback | Speech-to-text partials with a stop-word list | P2-07 | Later |
| DEC-068 | Spend cap before P1-04 | 60 requests an hour plus money caps plus a vendor console limit | P0-07, G1 | NEXT |
| DEC-080 | Heading direction | Counter-clockwise positive; left is positive | P4-01, P4-05 | Later |
| DEC-081 | Form of a frame id | Random UUID per tracking start or reset | hardening (10-F20), P4-01 | NEXT |
| DEC-082 | ARCore says "limited" | Treat as lost for anything that acts | P4-02, P4-06 | Later |
| DEC-083 | When a recovery counts as a jump | Allow 0.5 m plus walking distance for the gap | P4-02, G9 | Later |
| DEC-084 | Fallback for a silent map reset in Phase 4 | Build the home-marker part of P5-01 before G9 | G9 | Later |
| DEC-085 | Can a guest label a place | No | P4-04 | Later |
| DEC-086 | Save a place if its description fails | Yes, mark the description pending | P4-04 | Later |
| DEC-087 | "Safety line once per session" | After each start and every 4 hours | P4-06 | Later |
| DEC-088 | Invented distances: block or log | Block; the tool supplies the phrase | P4-07 | Later |
| DEC-089 | Guidance during an enrollment | Refuse | P4-06 | Later |
| DEC-090 | G8 and G9 gate wording | Adopt doc 40 section 9.5 | G8, G9 | Later |
| DEC-091 | When two claims are "disputed" | Only conflicting single-valued relations | P4-08 | Later |
| DEC-092 | Can Jeff read Syd's private notes | No; private means private | P4-08 | Later |
| DEC-093 | Status of a claim about oneself | Confirmed if said at confirmed band | P4-08 | Later |
| DEC-094 | Scope of "forget that" | The latest claim, read back first | P4-08 | Later |
| DEC-095 | Lowest band at which a claim is stored | Probable | P4-08 | Later |
| DEC-096 | Band for the permission question | Confirmed | P4-08 | Later |
| DEC-097 | What a session summary may contain | Claim ids only | P4-08 | Later |
| DEC-100 | X-01 in two stages | Yes | X-01 | NEXT |
| DEC-101 | Install development test tools | Yes, hash-pinned | X-01 stage 1 | NEXT |
| DEC-102 | Mutation tool on Windows | Scripted mutator first; try mutmut once | X-01 stage 1, gate step 0 | NEXT |
| DEC-103 | Reserved tool names in the harness manifest | Yes | X-01 stage 1 | NEXT |
| DEC-104 | Flood of valid frames | Per-type rate limit; `busy`; close on sustained abuse | hardening (H-09) | NEXT |
| DEC-105 | IPv6 loopback `[::1]` in phone settings | Refuse | hardening (T-AND-002) | NEXT |
| DEC-106 | Unknown keys in `config.toml` | Stop at start | hardening (H-05), P0-07 | NEXT |
| DEC-107 | Phone told to accept frames over 8 MiB | Refuse, as the schema does | hardening (T-P0-05-062) | NEXT |
| DEC-108 | Request log cannot be opened | Refuse to start; if it fails later, say so once and continue | P0-07 | NEXT |
| DEC-109 | G1 "hold up a sign" test | SimBody image at G1; camera at G2 | G1 | NEXT |
| DEC-120 | Formally defer the G0 anchor test to G8 | Yes, and add it to the G8 record | none (record) | NOW |
| DEC-121 | Small confirmations: Python 3.13, clock-stability definition, image size split | Yes to all | plan text | NOW |
| DEC-122 | G10 numbers: turns, sessions, cost ceiling | 50 turns, 3 labelled sessions, your cost figure | R-01, G10 | Later |
| DEC-123 | Body-agnostic proof with skipped steps; ReplayBody first | Yes and yes | G10 | Later |
| DEC-124 | Two demo runs vs the 10-minute greeting cool-down | A console `demo reset` | P5-04 | Later |

Count: 78 decisions on the phone-and-laptop path (18 identity and consent, 6 privacy, 2 behaviour and guidance safety, 19 voice, latency and cost, 18 spatial and memory, 10 test infrastructure, 5 plan text and gates); 13 of them block the next work (DEC-025, DEC-068, DEC-081, DEC-100 to DEC-109). Parked separately: 10 robot decisions (DEC-030 to DEC-039, last section). Total 88.

---

## 1. Identity and consent (DEC-001 to DEC-018)

| ID (aliases) | Question in plain language | Why it matters | Recommended answer | What depends on it | Blocks |
| --- | --- | --- | --- | --- | --- |
| DEC-001 (DEC-ID-01; doc 50 Q4, B-25) | Who may record that someone agreed to face and voice recognition, and how? | A family member can feel pressured to say yes | Only at the laptop console, with the person present, saying yes themselves; you type it and note a witness if one is there. The phone may suggest it, never record it. Ask each person separately with a one-page plain explanation | P3-01 consent gate; tests T-P3-01-010, T-P3-01-011, T-P3-01-012, T-P3-01-013, T-P3-01-014, T-P3-01-015 (doc 30) | Later: P3-01 |
| DEC-002 (DEC-ID-14; doc 40 40-F70; doc 00 section 16) | Is Syd under 18 (or the age of consent where you live)? | Consent from a minor alone may not count | If so, a parent or guardian records consent too, and Syd can still say no | P3-01 enrollment of Syd; T-P3-01-116 | Later: P3-01 (Syd only) |
| DEC-003 (DEC-ID-02; doc 50 B-25) | What happens on day 31 after someone's data was set aside for deletion? | Keeping deleted data longer than promised is the bigger harm | It can no longer be restored; the brain deletes it for good at its next start and writes a record. If the computer's date jumped forward more than `CLOCK_JUMP_MAX_DAYS`, it asks you first | P3-01 quarantine; T-P3-01-041 group | Later: P3-01 |
| DEC-004 (doc 50 Q6, C-15) | May anyone in the house say "stop recognising me" and have it take effect without proving who they are? | Requiring proof of identity to opt out of identification is backwards | Yes for pausing recognition; deleting still needs the console | P3-01, P3-11; T-SYS-051 | Later: P3-01 |
| DEC-005 (DEC-ID-10; doc 50 Q10, B-28, C-07) | Can one family member delete or enroll someone else from the phone? | Today the rule checks that someone is confirmed, not who | No. Deleting is allowed for you (owner) or the person whose data it is; starting an enrollment is yours only; everything else at the console | P3-10 `allowed_callers`; T-P3-10-050, T-P3-10-051, T-P3-10-052, T-P3-10-053; R-ID-55 | Later: P3-10 |
| DEC-006 (DEC-ID-08; doc 50 C-04 rule part) | Should sensitive requests from the phone require repeating two random words the robot says aloud? | A recording of your voice with you in view passes face plus voice; it cannot know fresh words | Yes, before any sensitive tool works from the phone | P3-10; T-P3-10-037, T-P3-10-038, T-P3-10-039; R-ID-57, R-ID-02 | Later: P3-10 |
| DEC-007 (DEC-ID-09; doc 50 B-27) | The plan mentions an "explicit second factor". What is it? | Nothing implements it; an undefined escape hatch is a hole | Remove the phrase; the console is the alternative when face and voice cannot both be confirmed | Plan P3-10 step 7 | Later: P3-10 (plan text) |
| DEC-008 (DEC-ID-06; doc 50 C-04 check part) | Build a "flat face" detector using the phone's depth sensor? | Photos are expected to fool the face matcher | Not for v1, as long as no phone-channel action is irreversible; build it before any such action is added | P3-04; optional card P3-12 | Later: P3-04 (only if an irreversible phone action is proposed) |
| DEC-009 (DEC-ID-11; doc 50 B-31) | In conversation mode, should the robot ignore an unknown voice or ask who it is? | The plan has both rules and they conflict | Ask once, then ignore unless spoken to again | P3-08 step 5 vs P3-07 step 6 | Later: P3-08 |
| DEC-010 (DEC-ID-16; doc 50 Q21; plan Q9) | Keep enrollment audio and photos, or templates only? | Fewer copies to protect and to delete | Templates only; re-enroll when needed | P3-03, P3-06; R-08 expects re-enrollment anyway | Later: P3-03 |
| DEC-011 (DEC-ID-07) | How many photo and screen attacks at G5? | Showing the weakness needs few; proving a defence needs many | The plan's 3 per type per enrolled person; 29 or more only if a defence is ever claimed | G5 script | Later: G5 |
| DEC-012 (DEC-ID-15) | For "stranger" tests, real volunteers or public data? | Volunteers' faces and voices are biometric data with nowhere to be deleted from today | Public licence-cleared photos and LibriSpeech voices only; volunteers only as registered evaluation-only people with consent and deletion | P3-04, P3-07 trials; R-ID-59 | Later: P3-04 |
| DEC-013 (doc 50 Q14, B-26) | Third-party faces in digiKam group photos and in recordings: what is allowed? | Non-consenting people's biometrics should never be created | Read only the tagged regions of consenting people, in place; never keep other crops; guests are never enrolled | P3-03; R-ID-22 | Later: P3-03 |
| DEC-014 (DEC-ID-17) | How often should the robot use a person's name? | Naturalness is a judgement; a number lets a monitor track drift | Report the share at G7 and choose then; start with "about one reply in four" as a guide, not pass or fail | P3-11 acceptance | Later: P3-11 |
| DEC-015 (doc 50 Q5, C-14) | Where do you live (state and country)? | Several places have biometric laws; household use may or may not be exempt | Record it in `docs/privacy.md` before enrolling anyone; if a biometric statute applies, get written consent and a deletion schedule, and consider a short check with a lawyer (this is not legal advice) | P3-01; T-SYS-050 | Later: P3-01 |
| DEC-016 (DEC-ID-04; doc 50 C-01 sandbox part) | Should image decoding run in a separate, sandboxed process? | A native decoder crash takes the brain down | Not for v1: header checks, size caps and fuzzing; revisit if fuzzing finds a crash | P1-01 media gate, P3-02 | Later: P1-01 |
| DEC-017 (DEC-ID-05) | How should the identity registry be encrypted? | File-level decryption to a temp file leaves plaintext behind | Keep SQLite in memory and save it as one encrypted file through the shared vault; no SQLCipher | P3-01 | Later: P3-01 |
| DEC-018 (DEC-ID-12) | Voice model files from SpeechBrain and NeMo can run hidden code when loaded. How to load them? | A swapped file could run code on your laptop | Convert once, under review, to ONNX or safetensors, record the hashes, never load the original formats at run time; skip NeMo unless converted | P3-05; R-SYS-03; T-P3-05-045, T-P3-05-046, T-P3-05-047 | Later: P3-05 |

## 2. Privacy and the company laptop (DEC-020 to DEC-025)

| ID (aliases) | Question in plain language | Why it matters | Recommended answer | What depends on it | Blocks |
| --- | --- | --- | --- | --- | --- |
| DEC-020 (DEC-BIOMETRIC-HOST, DEC-ID-13; doc 50 Q2) | May family face or voice templates, enrollment media or recordings of anyone but you exist on this company laptop at all? | The laptop is managed by your employer: disk recovery keys, management scripts, crash dumps and backups mean "never leaves the laptop" cannot be promised | No. Run enrollment, and keep any such data, only on your own machine after the move; until then the system is Jeff-only. If it must stay: a passphrase on the key, `data/` outside synced folders, the crash-dump check, ask IT what they collect, and write down that you accept the remaining risk. **Documents differed**: doc 00 says no; doc 30 says "preferably no" with that fallback. Pick: no, with the fallback only by a dated written acceptance | P3-01 first enrollment; Phase 2 sessions with others present; R-ID-56, R-SYS-02 | NOW (answer before P1-03) |
| DEC-021 (doc 50 Q1, B-51, C-12) | When does the laptop have to go back, and what will hold the project afterwards? | Moving before keys and biometrics exist means only code and config move | Set a date now; move to your own machine before any face or voice data exists; drill the migration on a second folder now | R-SYS-01, R-FND-12; T-SYS-010, T-SYS-011, T-SYS-012, T-SYS-013 | NOW |
| DEC-022 (DEC-VAULT-KEY; doc 50 C-06 key part) | How is the key for encrypted recordings and the registry protected: the Windows credential store (DPAPI) or a passphrase typed at start? | DPAPI protects against other local accounts, not against the employer | A passphrase-derived key (scrypt, as `tools/migrate.py`) for anything biometric; DPAPI is acceptable for Jeff-only data on this laptop. Record the choice in `docs/migration.md`. **Documents differed**: the earlier doc 50 C-06 said "key in the credential store via DPAPI"; doc 00 says passphrase for biometric data. Pick doc 00 (the employer can unprotect DPAPI secrets) | C-06 vault; P1-03 first recording; P3-01 | Later: P1-03 |
| DEC-023 (DEC-20-01, DEC-20-19, DEC-ID-03; doc 50 Q3, B-11) | What may be recorded, stored or sent to a vendor before the consent gate (P3-01) and the encrypted store exist? | Recordings of faces and voices are biometric data in all but name; guests never agreed | Recorder off by default; only a `jeff_only` mode switched on per session at the console, encrypted from the first byte or not written; sessions expire after 7 days unless you keep them; Phase 2 test sets use synthetic, licensed or your own audio; the wake-word trials before consent use you and synthetic voices only; never use a segment with an unknown person as test data | P1-03, P2-02, P2-04, P2-07; R-EYE-09, R-VOX-23, R-ID-58 | Later: P1-03 |
| DEC-024 (doc 50 Q7, B-20; plan Q3) | Is it acceptable that conversation audio goes to a cloud speech vendor? | Hands-free mode would send every household conversation | Yes for push-to-talk only until the wake word works; record the vendor's retention terms and opt out of training; local speech-to-text if not | P2-04, P2-07; R-VOX-19 | Later: P2-04 |
| DEC-025 (DEC-10-10; doc 50 B-10; Appendix A `REQUEST_LOG_RETENTION_DAYS`) | Should the request log keep full prompts (your words, transcripts, tool results)? | A full-prompt log is personal data on a company laptop | No full prompts until P2-04 sets a rule: log sizes, ids, model, cost and refusals. If you want full prompts later, keep them `REQUEST_LOG_RETENTION_DAYS` (14). **Documents differed**: doc 10 says do not log them; doc 00 Appendix A and the earlier B-10 proposed 14-day retention. Pick doc 10 now, Appendix A's 14 days if you opt in | P0-07 request log; T-P0-07-031 group | NEXT: P0-07 |

## 3. Behaviour and guidance safety on the phone path (DEC-040, DEC-041)

Both are Phase 5 phone-path decisions. The robot decisions DEC-030 to DEC-039 are parked in the last section.

| ID (aliases) | Question in plain language | Why it matters | Recommended answer | What depends on it | Blocks |
| --- | --- | --- | --- | --- | --- |
| DEC-040 (DEC-45-01; doc 50 Q12 quiet part, B-42, H-17) | Who may turn quiet mode off, and how is `set_quiet` classed? | Turning quiet off makes the agent do more, so a TV voice must not be able to do it | Anyone may turn quiet on. Only the console or a speaker confirmed as a household member may turn it off; never an unknown voice, a TV or an automatic event turn. Give the tool its own class `quiet` before the tool catalogue freezes at G2. Default off; keep the setting across restarts | P5-02; H-17 (class change before G2) | Later: P5-02 (class change before G2) |
| DEC-041 (DEC-45-03) | When position may have drifted (STALE), should guidance stop or continue with a warning? | Stopping at every small drift is annoying; continuing silently is unsafe | Continue with a spoken warning and ask for a marker look; refuse after twice the walk budget | P5-01; R-SPA-46 | Later: P5-01 |

## 4. Voice, latency and cost (DEC-050 to DEC-068)

| ID (aliases) | Question in plain language | Why it matters | Recommended answer | What depends on it | Blocks |
| --- | --- | --- | --- | --- | --- |
| DEC-050 (DEC-20-02) | What happens when someone talks past the 15-second limit (`UTTERANCE_MAX_S`)? | Audio could be lost or repeated at the cut | Continue in a second piece with no gap and no repeated audio; both pieces flagged `forced`; the end mark is the cut time | P2-03 | Later: P2-03 |
| DEC-051 (DEC-20-03; doc 50 B-18, B-56) | End-of-speech silence of 500 ms vs the 250 to 500 ms budget; where does a turn start for latency? | The latency budget does not add up otherwise | Keep 500 ms; call the stage "silence plus one detector window" (about 530 ms) and the budget "about 1.4 to 2.2 s"; a turn starts at the last speech sample (push-to-talk: release); compare G4 and G6 on that same mark; revisit after G3 | P2-03, P2-06, G4, G6 | Later: P2-03 |
| DEC-052 (DEC-20-04; doc 50 B-23) | What do "flush within 100 ms", "cancel within 200 ms" and "a quarter second" measure? | One number mixes the phone, the link, the brain and the loudspeaker | Separate figures: phone flush call time; brain-side send and close times on a fake clock; an acoustic end-to-end median 250 ms and 95th percentile 400 ms, always shown with the link-delay breakdown; small-n trials replaced by the doc 20 sample sizes | P2-01, P2-05, P2-07 | Later: P2-01 |
| DEC-053 (DEC-20-05; doc 50 B-24) | G4 interrupts with the push-to-talk button, which is only built in P2-07 (after G4). | G4 cannot run as written | G4 interrupts with a console key that calls the same cancel; push-to-talk barge-in is checked at P2-07 (G6); edit the plan. Alternative: move that P2-07 step before G4 | P2-05, G4 | Later: G4 |
| DEC-054 (DEC-20-06; doc 50 B-14) | Image policies: too many requests, unsolicited images, rotation, sizes above the native size | Tests cannot be written without a policy | Extra requests get `busy` with a reason; unsolicited images are dropped and counted (`UNSOLICITED_IMAGES_PER_MIN_MAX` closes the link); rotate pixels and strip EXIF; clamp to the native size, never upscale; capture time p50 under 150 ms and p95 under 250 ms | P1-01 | Later: P1-01 |
| DEC-055 (DEC-20-07) | Two model calls in flight together can overshoot the spend cap | "Cap plus one request" is otherwise unbounded | Reserve each call's worst-case cost before sending, so total spend never exceeds the cap | P1-04; R-EYE-07 | Later: P1-04 |
| DEC-056 (DEC-20-08; doc 50 B-12 decision part) | Do the speech vendors get their own spend caps? | Speech is metered but not capped in the plan | Yes: separate speech-to-text and text-to-speech caps enforced like the model cap | P1-04, P2-04, P2-05 | Later: P1-04 |
| DEC-057 (DEC-20-09) | What does the phone do if the link drops mid-sentence? | Old audio could replay on reconnect | Play at most `PLAY_BUFFER_MAX_MS` of what is buffered, then go silent; never replay old chunks | P2-01 | Later: P2-01 |
| DEC-058 (DEC-20-10) | What does cancel do to a tool call in progress? | Interrupted replies must not leave half-done actions | A non-motion tool finishes, its result is discarded and not spoken, history marks the turn interrupted; a motion tool is never started or stopped by a speech cancel | P2-05 | Later: P2-05 |
| DEC-059 (DEC-20-11) | Two replies are requested together | Overlapping audio | The newest cancels the older; never two voices at once | P2-05 | Later: P2-05 |
| DEC-060 (DEC-20-12) | How are numbers, dates, units and symbols spoken? | A test needs a fixed answer | A table in the speech sanitiser (dates and times in words, units expanded, digits grouped above 999); you review it at the P2-05 listening test | P2-05 | Later: P2-05 |
| DEC-061 (DEC-20-13) | Is the wake phrase kept in the transcript the agent sees? | It wastes tokens and can confuse the model | Strip it before the agent sees the text; keep it in the audio buffer | P2-07 | Later: P2-07 |
| DEC-062 (DEC-20-14) | How long does conversation mode stay open? | The agent's own speech must not keep the mic open | Each of your turns renews `ENGAGED_WINDOW_S`, up to `ENGAGED_MAX_S`; the agent's own speech never renews it | P2-07 | Later: P2-07 |
| DEC-063 (DEC-20-15) | May a voice command widen listening (for example "always listen")? | Widening by voice can be done by a TV | Voice may only narrow (mute, "be quiet", "stop"); widening only by button or console | P2-07 | Later: P2-07 |
| DEC-064 (DEC-20-16) | Own-voice guard on short utterances | A short real reply could be thrown away | Drop an utterance under `GUARD_MIN_WORDS` only on an exact match of the agent's last sentence within `GUARD_WINDOW_S` | P2-07 | Later: P2-07 |
| DEC-065 (DEC-20-17; doc 50 B-22) | Showing a last-turn total on the phone needs a display command that does not exist | The plan step cannot be built | Drop the step; the phone measures release-to-playback on its own clock and reports it in an event | P2-06 | Later: P2-06 |
| DEC-066 (DEC-20-18; doc 50 B-13) | Default phone temperature limit, and where is it enforced? | The phone must protect itself with the brain absent | `BATTERY_TEMP_MAX_C` 40 C until G2 gives real numbers; the phone enforces the timer and the limit; the phone app cannot extend them; a persistent notification shows capture state. **Documents differed**: Appendix A had 42 C; doc 20 recommended 40 C. Pick 40 C (the safer value; Appendix A updated) | P1-05 | Later: P1-05 |
| DEC-067 (DEC-20-20) | How is "stop" heard while the agent is speaking? | The agent hears itself | Speech-to-text partials during playback with a short stop-word list on gated, higher-bar audio; a keyword spotter only if that proves too slow | P2-07; C-16 | Later: P2-07 |
| DEC-068 (DEC-10-08; doc 50 Q8, B-09, C-09; plan Q5) | What hard spend cap applies before the full budget guard (P1-04) exists? | P0-07 calls the model API with no cap until P1-04 | All three: at most `INTERIM_MAX_REQUESTS_PER_HOUR` (60) model requests per hour, failing closed; small session and daily money caps in `config.toml` (your amounts, bounded by `SPEND_CAP_SANITY_MAX`); `max_tokens` on every request; and a spend limit set in the vendor console as a second wall. **Documents differed**: doc 10 proposed a request-count cap; the earlier doc 50 proposed money caps; they are complementary, so both | P0-07, G1; R-FND-37, R-SYS-04 | NEXT: P0-07 |

## 5. Spatial and memory (DEC-080 to DEC-097)

| ID (aliases) | Question in plain language | Why it matters | Recommended answer | What depends on it | Blocks |
| --- | --- | --- | --- | --- | --- |
| DEC-080 (DEC-HEADING; doc 50 Q9, B-34) | Which way is positive for heading and for left and right? | One sign error mirrors every spoken direction | Counter-clockwise positive seen from above, for heading and for the relative bearing: a left turn is positive, right is negative, as the protocol (section 4.4) and the code (`Pose.heading_rad`) already do. Edit plan P4-01 ("positive clockwise") and P4-05 ("left negative"). **Documents differed**: the earlier doc 50 B-34 proposed a right-positive bearing; C12 and doc 40 section 0.5 use left-positive. Pick left-positive (one convention, no negation anywhere) | P4-01, P4-05, P4-06, P5-01 oracles; R-SPA-01, R-SPA-26 | Later: P4-01 |
| DEC-081 (DEC-SPA-01; doc 10 10-F20) | What form must a map frame id have? | SimBody restarts its ids at `sim-1`, so two sessions share an id and a saved place matches the wrong map | A fresh random UUID (version 4) every time tracking starts or resets, on the phone and in SimBody; change the protocol example `world-1`. **Documents differed** slightly: doc 10 suggested `sim-<boot8>-<n>`; doc 40 a UUID. Both are unique; pick the UUID (one form everywhere) | Hardening fix of 10-F20 (T-P0-06-075), P4-01 (T-P4-01-017); R-FND-57 | NEXT: hardening |
| DEC-082 (DEC-SPA-02) | What happens when ARCore says `limited`? | Guidance on degraded tracking | Treat it like lost for anything that acts (guidance pauses, saving a place is refused); it still speaks the tip; degrade at once, resume only after ok has held for `RECOVER_DWELL_MS` | P4-02, P4-06 | Later: P4-02 |
| DEC-083 (DEC-SPA-03; doc 50 B-35) | When does a recovery count as a "jump"? | The plan's rule makes G9 step 3 fail by design | Allow `MAX_JUMP_M` plus walking distance at `RECOVERY_WALK_MPS` for the gap; also mark the map suspect after `FAULT_CLUSTER_N` short faults within `FAULT_CLUSTER_WINDOW_S` (the G0 pattern) | P4-02, G9 | Later: P4-02 |
| DEC-084 (DEC-SPA-04; doc 50 C-19) | Phase 4 has no defence against a silent drift or a reset that looks continuous. What fallback? | A wrong position spoken confidently | Build the home-marker part of P5-01 before G9; until then G9 records "suspect frame" as possible and you watch for it | G9; R-SPA-07, R-SYS-09 | Later: G9 |
| DEC-085 (DEC-SPA-05) | Can an unknown speaker (a guest) label a place? | `created_by` must be a person | No: labelling needs a known speaker at probable or better | P4-04 | Later: P4-04 |
| DEC-086 (DEC-SPA-06) | If the description call fails, is the place saved? | Losing the place is worse than a missing description | Yes: save the pose and photo with `description_pending`; retry later | P4-04 | Later: P4-04 |
| DEC-087 (DEC-SPA-07) | What does "safety line once per session" mean? | A test needs a rule | Once after each brain start, and again after `SAFETY_LINE_REPEAT_H` hours | P4-06 | Later: P4-06 |
| DEC-088 (DEC-SPA-08; doc 50 B-38) | Does the "no invented distances" check block speech or only log it? | A prompt-only rule is not a control | Block. The tool supplies the spoken phrase ("about 10 feet"); an answer with any other number is sent back once, then spoken with the number removed | P4-07 | Later: P4-07 |
| DEC-089 (DEC-SPA-09) | Can guidance start during an enrollment? | Two flows would fight over the speaker | No: refuse with "let's finish first" | P4-06 | Later: P4-06 |
| DEC-090 (DEC-SPA-10; doc 50 B-36, B-37) | Plan gate wording for G8 and G9 | As written, G8 step 4 needs P4-07 and G9 step 3 fails by design | Adopt the wording in doc 40 section 9.5 (within one phone session until P5-01; resume only when the frame is not suspect) | G8, G9 | Later: G8 |
| DEC-091 (DEC-MEM-01; doc 50 B-40) | When are two remembered claims "disputed", and how does one leave that state? | "Disputed" is undefined in the plan | Only when they give different values for the same single-valued relation between the same two people; free text is never disputed; the subject's yes or no settles it | P4-08 | Later: P4-08 |
| DEC-092 (DEC-MEM-02; doc 50 Q11, B-39) | The plan says notes are "private to the speaker and Jeff". Can you read Syd's private notes? | It conflicts with per-person privacy and with "Jeff has no special power on the phone" | No. Private means private to the subject and the person who said it; no phone-channel exception for you; console export only | P4-08; R-MEM-06 | Later: P4-08 |
| DEC-093 (DEC-MEM-03) | What status does a claim about oneself get? | Self-claims are common | Said at confirmed band: confirmed (method `self`); at probable: provisional until repeated at confirmed band | P4-08 | Later: P4-08 |
| DEC-094 (DEC-MEM-04) | What do "forget that" and "forget everything about me" cover? | Forgetting the wrong thing, or too little | "Forget that" is the latest claim the person made or the agent used with them, read back first; "everything about me" covers claims about and by the person, their copies, and any provisional record | P4-08; R-MEM-11 | Later: P4-08 |
| DEC-095 (DEC-MEM-05) | Lowest confidence at which a claim is stored | Guests and uncertain speakers must not create memories | Probable or better, with the band recorded; uncertain or unknown stores nothing | P4-08 | Later: P4-08 |
| DEC-096 (DEC-MEM-06) | Does the permission question ("is it all right if I ask...") itself need the confirmed band? | Asking the wrong person is the harm | Yes | P4-08; R-MEM-12 | Later: P4-08 |
| DEC-097 (DEC-MEM-07) | What may a session summary contain, and is it rewritten after a forget? | A summary could leak a private claim | Claim ids only, never claim text; then a forget needs no rewrite | P4-08 | Later: P4-08 |

## 6. Test infrastructure and the Phase 0 platform (DEC-100 to DEC-109)

| ID (aliases) | Question in plain language | Why it matters | Recommended answer | What depends on it | Blocks |
| --- | --- | --- | --- | --- | --- |
| DEC-100 (doc 50 Q16, B-07; doc 10 9.11) | Accept X-01 (the chaos and boundary harness) in two stages? | Parts of X-01 need cards that do not exist yet | Yes: stage 1 after P0-06 (everything not needing later cards); stage 2 obligations attach to P0-07, P1-03, P3-01, P3-10 and P4-08 | X-01 acceptance | NEXT: X-01 stage 1 |
| DEC-101 (DEC-DEV-DEPS, DEC-10-05) | Install the development tools the test designs assume? | Without them property, fuzz, soak, security and dependency tests cannot run; a missing tool must fail, not skip | Yes, with a hash-pinned dev lockfile and a `docs/licences.md` line each: Hypothesis, psutil, Bandit, pip-audit and a secret scanner now (X-01 stage 1); numpy and Pillow at P1-01; model runtimes at their cards | E7, E10, PB and SOAK tests; T-P0-04-038 | NEXT: X-01 stage 1 |
| DEC-102 (DEC-MUTATION-TOOL, DEC-10-11) | Which mutation-testing tool, given mutmut on native Windows is unverified? | No mutation score means anything until a tool runs | A small stdlib `ast` mutator (comparison flip, off-by-one at bounds, constant swap, deleted `raise`) on the six security modules first; try mutmut once in X-01 stage 1 (T-X01-006); cosmic-ray as a second option. **Documents differed** in order: doc 00 "try mutmut first", doc 10 "scripted first". Pick scripted first (it certainly runs), mutmut as a trial | Gate step 0; `MUTATION_SCORE_MIN` | NEXT: X-01 stage 1 |
| DEC-103 (DEC-10-06) | Are the reserved tool names entry points in the harness manifest? | A reserved name that becomes a tool must already be tested as refused | Yes | X-01 manifest | NEXT: X-01 stage 1 |
| DEC-104 (DEC-10-01; doc 50 Q12 flood part, B-59, H-09) | What happens when the phone floods the brain with valid messages? | Valid heartbeats and 8 MiB echoes are answered without limit today | Token bucket per message type (`PEER_MSG_MAX_PER_S`); excess answered `busy`; sustained excess closes the link | H-09; T-P0-05-040, T-P0-05-041, T-P0-05-042 | NEXT: hardening |
| DEC-105 (DEC-10-02) | Is an IPv6 loopback address (`[::1]:8765`) accepted in the phone's link settings? | The loopback policy lists only 127.0.0.1 and localhost | Refuse | T-AND-002 | NEXT: hardening |
| DEC-106 (DEC-10-03; doc 50 Q13, B-58, H-05) | Unknown keys in `config.toml`: stop or warn? | A misspelt cap name would silently remove the cap | Stop at start (fail closed) | H-05; T-P0-04-009 | NEXT: hardening (before P0-07) |
| DEC-107 (DEC-10-04) | The phone is told to accept binary frames above 8 MiB: shrink the value or refuse? | Python refuses; Kotlin silently coerces | Refuse, matching the schema | T-P0-05-062 | NEXT: hardening |
| DEC-108 (DEC-10-07) | The request log cannot be opened: refuse to start? | Spend without a record | Refuse at start; if it fails while running, say so once and continue | T-P0-07-031 | NEXT: P0-07 |
| DEC-109 (DEC-10-09; doc 50 B-08) | G1's "hold up a sign reading delete everything" needs an image path P0-07 does not have; the typed version goes through the privileged console | The gate tests the wrong thing as written | At G1, a SimBody image carries the sign and the phrases go through the app channel (T-GATE1-001, T-GATE1-002); the real camera version moves to G2 | G1 script | NEXT: G1 (right after P0-07) |

## 7. Plan text and gates (DEC-120 to DEC-124)

| ID (aliases) | Question in plain language | Why it matters | Recommended answer | What depends on it | Blocks |
| --- | --- | --- | --- | --- | --- |
| DEC-120 (doc 50 Q17, B-04) | Accept formally that the G0 anchor-drift test is deferred to G8? | GO was recorded on an unmeasured criterion (R-FND-06, score 16) | Yes: add a deviation line to G0 and the measurement (T-P0-03-003) to the G8 record, owner Jeff; this is the waiver for R-FND-06 | G0 record, G8 record | NOW (record) |
| DEC-121 (doc 50 Q18, B-01, B-05, B-54) | Small confirmations: Python 3.13 in the plan; the clock-stability definition from protocol section 6 (max change under 20 ms in any 60 s window after 60 s warm-up); the image size split (2 MiB for phone stills, 8 MiB for brain-side files) | Plan and code disagree today | Yes to all three | Plan text; Appendix A `MAX_IMAGE_BYTES`, `MAX_IMAGE_FILE_BYTES`, `CLOCK_STABILITY_MAX_MS` | NOW (plan text) |
| DEC-122 (DEC-45-04; doc 50 Q19, B-45) | G10: how many turns and labelled sessions decide it; the running-cost ceiling; what happens when only spatial, body-agnostic or cost fails? | Two runs cannot support a rate | At least `GATE_MIN_UTTERANCES` (50) turns and `GATE_MIN_LABELLED_SESSIONS` (3); you write your cost ceiling per 10-minute session in `g10_inputs.toml`; spatial or body-agnostic failing means NO-GO, cost failing means ADJUST. **Documents differed**: Appendix A had 30 turns; doc 45 and the earlier doc 50 50. Pick 50 (Appendix A updated) | R-01 evaluator, G10 | Later: G10 |
| DEC-123 (DEC-45-05; doc 50 Q19, B-45) | Can the body-agnostic proof pass with steps SKIPPED on a body that cannot do them, and must ReplayBody exist first? | Today's ReplayBody stub makes the criterion impossible | Yes to SKIPPED steps when each has a recorded reason and every other step passes; yes, ReplayBody (P1-03) before G10 | G10 | Later: G10 |
| DEC-124 (DEC-45-02; doc 50 B-43) | Two back-to-back demo runs vs the 10-minute greeting cool-down | The second run would fail for a reason that is not a fault | A console `demo reset` that clears cool-downs without restarting anything | P5-04 | Later: P5-04 |

## Where documents disagreed, and the pick

| Question | Document A says | Document B says | Pick and reason |
| --- | --- | --- | --- |
| DEC-020 biometric data on the laptop | Doc 00: no | Doc 30: preferably no, else passphrase plus written acceptance | No; the fallback only with a dated written acceptance (keeps the safe default, allows a documented exception) |
| DEC-022 vault key | Earlier doc 50 C-06: DPAPI credential store | Doc 00: passphrase for biometric data | Passphrase for biometric data (the employer can unprotect DPAPI secrets) |
| DEC-025 full-prompt log | Doc 10: do not log full prompts | Doc 00 Appendix A, earlier B-10: keep 14 days | Do not log until P2-04; 14 days if Jeff opts in (less personal data by default) |
| DEC-066 temperature limit | Appendix A: 42 C | Doc 20: 40 C | 40 C (safer; Appendix A updated) |
| DEC-068 interim spend cap | Doc 10: 60 requests an hour | Earlier doc 50: session and daily money caps plus vendor limit | Both (they protect against different failures) |
| DEC-080 relative bearing sign | Earlier doc 50 B-34: right positive | C12, doc 40 0.5: left positive | Left positive (one convention end to end; the earlier text is superseded) |
| DEC-081 frame id form | Doc 10: `sim-<boot8>-<n>` | Doc 40: UUID version 4 | UUID (one form for every body) |
| DEC-102 mutation tool order | Doc 00: try mutmut, then a scripted runner | Doc 10: scripted first | Scripted first (certain to run on Windows), mutmut as a one-off trial |
| DEC-122 G10 turns | Appendix A: 30 | Doc 45, earlier doc 50: 50 | 50 (Appendix A updated) |

## Old IDs to merged IDs

| Old | Merged |
| --- | --- |
| DEC-HEADING, DEC-BIOMETRIC-HOST, DEC-VAULT-KEY, DEC-MUTATION-TOOL, DEC-DEV-DEPS, DEC-LINUX-TARGET | DEC-080, DEC-020, DEC-022, DEC-102, DEC-101, DEC-039 |
| DEC-10-01 to DEC-10-11 | DEC-104, DEC-105, DEC-106, DEC-107, DEC-101, DEC-103, DEC-108, DEC-068, DEC-109, DEC-025, DEC-102 |
| DEC-20-01 to DEC-20-20 | DEC-023, DEC-050, DEC-051, DEC-052, DEC-053, DEC-054, DEC-055, DEC-056, DEC-057, DEC-058, DEC-059, DEC-060, DEC-061, DEC-062, DEC-063, DEC-064, DEC-065, DEC-066, DEC-023, DEC-067 |
| DEC-ID-01 to DEC-ID-17 | DEC-001, DEC-003, DEC-023, DEC-016, DEC-017, DEC-008, DEC-011, DEC-006, DEC-007, DEC-005, DEC-009, DEC-018, DEC-020, DEC-002, DEC-012, DEC-010, DEC-014 |
| DEC-SPA-01 to DEC-SPA-10 | DEC-081 to DEC-090 |
| DEC-MEM-01 to DEC-MEM-07 | DEC-091 to DEC-097 |
| DEC-45-01 to DEC-45-14 | DEC-040, DEC-124, DEC-041, DEC-122, DEC-123, DEC-030, DEC-031, DEC-032, DEC-033, DEC-034, DEC-035, DEC-036, DEC-037, DEC-038 |
| Earlier doc 50 Q1 to Q21 | Q1 DEC-021; Q2 DEC-020; Q3 DEC-023; Q4 DEC-001; Q5 DEC-015; Q6 DEC-004; Q7 DEC-024; Q8 DEC-068; Q9 DEC-080; Q10 DEC-005; Q11 DEC-092; Q12 DEC-040 (quiet) and DEC-104 (flood); Q13 DEC-106; Q14 DEC-013; Q15 DEC-030 (cap) and DEC-032 (link); Q16 DEC-100; Q17 DEC-120; Q18 DEC-121; Q19 DEC-122 and DEC-123; Q20 DEC-033; Q21 DEC-010 |
| Earlier doc 50 B-items marked "Decision" | B-04 DEC-120; B-05 DEC-121; B-07 DEC-100; B-08 DEC-109; B-09 DEC-068; B-10 DEC-025; B-11 DEC-023; B-12 DEC-056; B-13 DEC-066; B-14 DEC-054; B-18 DEC-051; B-20 DEC-024; B-22 DEC-065; B-23 DEC-052; B-24 DEC-053; B-25 DEC-001 and DEC-003; B-26 DEC-013; B-27 DEC-007; B-28 DEC-005; B-31 DEC-009; B-34 DEC-080; B-35 DEC-083; B-36 and B-37 DEC-090; B-38 DEC-088; B-39 DEC-092; B-40 DEC-091; B-42 DEC-040; B-43 DEC-124; B-45 DEC-122 and DEC-123; B-46 DEC-030 and DEC-031; B-47 DEC-032; B-54 DEC-121; B-56 DEC-051; B-58 DEC-106; B-59 DEC-104 |

## Parked: robot (not before the buy decision)

These ten decisions belong to the robot cards R-02 to R-09 and gate G11, which are parked until R-01 (the buy or no-buy memo at G10) says buy, and by Jeff's scope note not before face and voice recognition, history tracking and conversation over built knowledge are robust. Their IDs are kept. None of them blocks phone-path work, appears in the top 10, or appears in the near-term sequencing. R-01 itself (the evaluator) stays active because it runs at G10 on phone-path evidence.

| ID | Question (short) | Recommended | Blocks | Level |
| --- | --- | --- | --- | --- |
| DEC-030 | First robot speed cap | 0.25 m/s in the motor controller | R-02, G11 | Parked |
| DEC-031 | What "attended" and "night" mean | Hold-to-run button; 08:00 to 20:00 by the robot's clock | R-02 | Parked |
| DEC-032 | Robot link before encryption; bridge design | Wired or point-to-point; own small bridge, no rosbridge | R-05 | Parked |
| DEC-033 | Second reviewer for robot safety | Name a person before buying hardware | R-01, R-02, G11 | Parked |
| DEC-034 | Who may commit a re-anchored robot pose | Console, or confirmed Jeff with read-back | R-06 | Parked |
| DEC-035 | Evidence to lift "capped and supervised" | 59 clean trials per safety function; 299 runs before no held e-stop | after G11 | Parked |
| DEC-036 | ROS 2 distro for the kit | Jazzy or newer, or a tested upgrade path | R-01, R-04 | Parked |
| DEC-037 | Voice "stop" while driving | Keep a keyword stop as a convenience; e-stop is the real stop | R-08 | Parked |
| DEC-038 | Robot on the home network | Separate guest network or VLAN | R-03 | Parked |
| DEC-039 | Where ROS 2 integration runs | A separate Linux machine you own | R-04, R-05 | Parked |

| ID (aliases) | Question in plain language | Why it matters | Recommended answer | What depends on it | Blocks |
| --- | --- | --- | --- | --- | --- |
| DEC-030 (DEC-45-06; doc 50 Q15 cap part, B-46) | First robot speed cap, and how it may be raised? | "Walking pace" is about 1.4 m/s, far too fast in a house with a pet | 0.25 m/s (`ROBOT_SPEED_CAP_MPS`), set in the motor controller; raised only by a written decision citing at least 59 clean trials of each safety function | R-02, G11; R-ROB-01, R-ROB-02 | Parked: R-02 |
| DEC-031 (DEC-45-07; doc 50 B-46 part) | What do "attended" and "night" mean, and where are they enforced? | Software cannot know someone is watching | Attended: you hold a hold-to-run button the motor controller reads. Night: outside 08:00 to 20:00 by the robot's own clock; unknown time means no driving; the laptop cannot change either | R-02; R-ROB-15 | Parked: R-02 |
| DEC-032 (DEC-45-08; doc 50 Q15 link part, B-47) | Until the robot link is encrypted, wired or WiFi? Which bridge design? | Protocol v0 checks identity once and does not protect each message: on WiFi another device could inject a "drive" command | Wired or point-to-point only until protocol v1 has TLS or per-message authentication; a small robot-side bridge speaking the project's own protocol, not a generic rosbridge | R-05; R-SYS-16, R-ROB-10 | Parked: R-05 |
| DEC-033 (DEC-45-09; doc 50 Q20) | Who is the second reviewer for the robot safety rules, procedures and G11? | One person reviewing his own safety case is the weakest link | Name a person before buying hardware | R-01, R-02, G11 | Parked: R-01 |
| DEC-034 (DEC-45-10) | Who may commit a re-anchored robot pose? | A wrong pose sends the robot where it must not go | Console only, or a speaker confirmed as you, with a spoken read-back | R-06; R-ROB-27 | Parked: R-06 |
| DEC-035 (DEC-45-11) | What evidence lifts "capped and supervised" after G11? | Ten runs bound the failure rate only below about 26 percent | 59 clean trials of each safety function and 59 clean runs for any cap rise; 299 runs before considering operation without a held e-stop; each written up | After G11; R-ROB-26 | Parked: after G11 |
| DEC-036 (DEC-45-12) | Must the robot kit support ROS 2 Jazzy or newer? | Humble ends 31 May 2027 | Yes, or a written, tested upgrade path | R-01, R-04; R-ROB-23 | Parked: R-01 |
| DEC-037 (DEC-45-13) | On the robot, listen only when stopped, or keep a keyword "stop" while driving? | Voice stop is not a safety function either way | Keep a simple keyword "stop" while driving as a convenience; the physical e-stop is the real stop | R-08 | Parked: R-08 |
| DEC-038 (DEC-45-14) | Where does the robot live on the home network? | Vendor images may phone home and expose services | A separate guest network or VLAN with no route to the laptop's services until transport security exists | R-03; R-ROB-21 | Parked: R-03 |
| DEC-039 (DEC-LINUX-TARGET) | Where do Phase 6 simulation and ROS 2 integration run? | ROS 2 needs Linux; WSL needs admin rights this laptop does not have | A separate Linux machine you own, chosen at R-01; nothing robot-related on this laptop | R-04, R-05 test plans | Parked: R-04 |
