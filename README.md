# Embodied Agent

A trial of a Claude agent with a body: first an Android phone, later possibly a wheeled robot. The full plan is in `EmbodiedAgent-Plan-and-Implementation-Order.md`.

**Starting a session? Read [`docs/STATUS.md`](docs/STATUS.md) first:** what is built, what is next and what is waiting on whom.

## Layout

| Folder | What |
| --- | --- |
| `body-android/` | The Android app (Kotlin). The phone's eyes, ears, mouth and pose sensor |
| `brain/` | The Python brain on the laptop. Body-agnostic; only `brain/body/phone.py` may mention the phone. It reaches its body through the `Body` interface (`docs/adding_a_body.md`) |
| `protocol/` | The body-to-brain WebSocket protocol |
| `tools/` | Command-line tools (for example data migration) |
| `docs/` | Design notes, gate records, privacy and migration docs |
| `data/` | Private runtime data. Git-ignored and created locally |
| `profiles/` | Other people's setups (`docs/profiles.md`). Git-ignored |

## Setup (Windows, PowerShell)

```powershell
py -3.13 -m venv .venv    # Python 3.13 or newer
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env     # then fill in keys; .env is git-ignored
```

`requirements.lock` pins exact versions and hashes (Windows, Python 3.13, all extras including the large GPU libraries). To install from it: `pip install --require-hashes --only-binary=:all: -r requirements.lock`, then `pip install --no-deps -e .`. An install from this file has not yet been tried end to end. After changing a dependency, run `tools\make_lockfile.py`.

## Run the brain tests

```powershell
.\.venv\Scripts\python -m pytest -q
```

The protocol tests check the schema, every example in `protocol/v0.md`, the binary frame layout, the server against a Python test body, and that the body's Kotlin allowlist matches the spec. The body tests run one shared contract (`tests/body_contract.py`) against the simulated body and against the phone body over loopback (no phone needed). The tool tests check the agent's tool schemas and that every request outside the allowlist is refused and logged. The whole suite runs under the egress guard, so no test can reach the network.

Type checking (mypy, strict, over the packages listed in `pyproject.toml` `[tool.mypy] files`; also run by `tests/test_typing.py`):

```powershell
.\.venv\Scripts\python -m mypy
```

## Chaos tests

```powershell
.\.venv\Scripts\python -m brain chaos           # quick run
.\.venv\Scripts\python -m brain chaos --full    # 5 seeds, about 16 minutes; step 0 of every gate from G1
```

Hostile and failing input against every entry point. See `docs/chaos.md`.

## Run the brain

```powershell
.\.venv\Scripts\python -m brain
```

Prints the version, loads `config.toml`, and says which API keys are present or missing (never their values).

## Check a body and the agent's tools

```powershell
.\.venv\Scripts\python -m brain body-check --body sim
```

Starts a body, calls every agent tool through the tool registry (the same path the agent loop uses) and tries the command-boundary requests ("delete the C drive", "read config.toml" and so on), which must all be refused. `--body sim` is a scripted body that needs no phone and no network; `--body phone` listens for the phone (press Connect in the app; `--wait 60` sets how long to wait; stop `serve` first, since both use the same port). Without `--body` it uses `config.toml` `[body] kind`.

The agent has no tools except those listed in `brain/agent/tools.v0.json`; `docs/body_tools_egress_v0.md` describes the registry, the refusal format and the egress guard. The brain can only reach the hosts in `config.toml` `[egress]` and loopback ports it listens on itself: `brain/egress.py` refuses every other connection in the socket layer, removes proxy environment variables at start, and cannot be widened once installed.

## Talk to the agent loop

```powershell
.\.venv\Scripts\python -m brain chat
```

The agent loop (card P0-07) against SimBody with a scripted stand-in model: no key, no network, no spend. Type a line (or `/poke`, `/look <question>`, `/quit`); the reply is spoken through the body and the live event trace shows every step. `--as-app` feeds the lines as app-channel (untrusted) turns; `--live` uses the real model API and needs `ANTHROPIC_API_KEY`, `[models] allow_live_calls = true` and the spend caps in `[caps]`. Details: `docs/agent_loop.md`.

## Speech to text (local)

```powershell
.\.venv\Scripts\python -m pip install -e ".[stt]"                       # faster-whisper and friends (CPU, versions pinned)
.\.venv\Scripts\python -m pip install -e ".[stt-gpu]"                   # optional: the CUDA 12 libraries for an NVIDIA GPU (about 1.6 GB)
.\.venv\Scripts\python tools\fetch_stt_model.py                          # one-time, verified download of base.en and small.en (about 630 MB)
.\.venv\Scripts\python -m brain stt-check some-speech.wav --model base.en # 16 kHz mono 16-bit WAV, at most 15 s; prints device, compute type, load, warm-up and decode times
.\.venv\Scripts\python -m brain stt-check some-speech.wav --device cuda   # insist on the GPU (exit 2 with the reason if it is not usable)
.\.venv\Scripts\python -m pytest tests\test_stt_real_model.py tests\test_stt_gpu_real.py --run-real-model   # the tests that load the real models (the GPU ones skip without a CUDA device)
```

**GPU.** An NVIDIA GPU needs a CUDA 12 capable driver (525 or newer; this laptop has 621.24) and the one-time `pip install -e ".[stt-gpu]"` (cuBLAS, cuDNN and NVRTC, about 1.6 GB of downloads). Nothing else to set up: the engine finds the DLLs by itself. In `config.toml` `[stt]`, `device = "auto"` uses the GPU only if a one-time self-test passes and otherwise falls back to the CPU (the health panel says why); `"cuda"` insists. Check it with `python -m brain stt-check FILE.wav`: the first line shows `device cuda int8_float16`. Measured on this laptop (RTX 3080 Laptop, six synthetic sentences, warm median decode): base.en 73 ms on the GPU against 697 ms on the CPU; small.en 141 ms against 2,171 ms. With the GPU `small.en` meets the plan's 300 ms bar; on a CPU-only machine use `base.en`.

Transcription runs on this laptop with faster-whisper (card P2-04, STT half); no audio and no text leaves it, and the brain never uses the network for it. The models live in `data/models/stt/` (git-ignored) and are checked against `lock.json` before every load. On this CPU a short utterance takes about 0.5 s with `base.en` and about 1.5 s with `small.en`, so the plan's 300 ms median target is not met on the CPU. Everything (config keys, how it is verified, measured numbers, the GPU and driver note, limits): `docs/stt.md`.

## Profiles: a setup for each person

```powershell
.\.venv\Scripts\python -m brain profile create mary        # make profiles\mary\ (own port, caps, .env, data; capture off)
.\.venv\Scripts\python -m brain profile list               # name, port, live calls, key present or missing, capture, last used
.\.venv\Scripts\python -m brain profile show mary          # the effective config, no secrets
.\.venv\Scripts\python -m brain info --profile mary        # key present or missing, and which file it would come from
.\.venv\Scripts\python -m brain chat --profile mary        # any command takes --profile NAME
```

A profile is another person's own config, API key (only from `profiles\NAME\.env`; the environment and Jeff's `.env` are never used for it), spend counters, logs, prompt capture and link token. No `--profile` is the default setup, unchanged. It separates configuration and data on one machine; it is not a security boundary between Windows users. How to get her own key, what is shared, and how to remove a profile: `docs/profiles.md`.

## Run the brain server (protocol v0)

```powershell
.\.venv\Scripts\python -m brain serve
```

Listens on `ws://127.0.0.1:8765/` (from `config.toml` `[network]`), runs `adb reverse tcp:8765 tcp:8765` so the phone can reach it and re-runs it every minute (skip with `--no-phone-link`), and logs to the console and to `data/logs/serve-<time>.log` (rotated at 10 MB): connections, the clock offset once a minute, echo-test results and any refused message. `-v` logs every clock sample and event payloads. Stop with Ctrl+C (or Ctrl+Break): connections close with 1001 and each session's clock summary is logged. A non-loopback `--host` is refused unless you add `--allow-remote`. The protocol is specified in `protocol/v0.md`.

Bodies must be paired: on first start the brain creates a random token in `%LOCALAPPDATA%\EmbodiedAgent\secrets\link_token` (outside the repo, in a folder made private to your account; `config.toml` `[paths] secrets_dir` can move it). The brain refuses a token file anyone else owns or can read, and says what to do. `python -m brain info` prints the location. Give it to the phone once, with a debug build of the app installed (the token goes to adb on stdin, never on a command line). `pair --rotate` makes a new token first; restart `serve` afterwards. The handshake proves both ends once but does not encrypt or protect each message, so it is only meant for loopback and the adb link (see `protocol/v0.md` section 4.2):

```powershell
.\.venv\Scripts\python -m brain pair
```

To redo only the phone link (for example after the adb connection drops): `.\tools\phone-link.ps1`.

To check the server from the laptop alone, run a test body against it (1,000 echoes of 100 kB by default):

```powershell
.\.venv\Scripts\python -m brain probe --count 1000 --bytes 102400
```

The probe connects as a body, so it replaces a connected phone (which then stops until Connect is pressed again). The egress guard lets it dial the loopback port it is given; `probe --host <another machine>` is refused unless that host and port are listed in `config.toml` `[egress]`.

## Build the Android app

First create `body-android/local.properties` containing `sdk.dir=C\:/Users/<you>/AppData/Local/Android/Sdk` (it is git-ignored). The build uses the JDK bundled with Android Studio, so adjust the path below if Studio is installed elsewhere.

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
cd body-android
.\gradlew.bat assembleDebug
```

Install on a paired phone with `adb install -r app\build\outputs\apk\debug\app-debug.apk`. Run the Kotlin unit tests (binary frame format, authentication vectors, clock estimator, backpressure, validation) with `.\gradlew.bat testDebugUnitTest`, and Android lint with `.\gradlew.bat lintDebug lintRelease`.

The app opens on the brain-link screen: a `host:port` setting (default `127.0.0.1:8765`), a link-state indicator (connected, retrying, stopped, not paired), Connect and Stop buttons, a Poke button, an echo-test button and an "ARCore check" button for the P0-02 checker. Poke sends a `poke` event (at most one every 3 s; a poke made too soon, or while not connected, is dropped and noted in the on-screen log, never queued), which makes the brain run a turn. Nothing runs until Connect is pressed. The link then runs in a foreground service with a notification (with a Stop action), so it stays up with the screen locked and reconnects by itself when the brain restarts. It stops itself after 3 minutes without a stable link (one that stayed up 10 s), when the app is swiped away, when the brain shuts down and does not return within a minute, or when another body replaces it; authentication failures are retried within those 3 minutes; it holds a CPU wake lock only while connected. The screen is kept on only during an echo test.

**Speech (P0-07).** The phone speaks `speak_local` text from the brain with Android TextToSpeech (voice-communication audio usage, transient audio focus). The engine starts when Connect is pressed and is released when the link stops. The hello lists `speak_local` only when the engine is initialised and usable (no voice data, or an engine that fails to start, means it is left out and the brain has no `speak_text`). The payload is checked exactly as in `protocol/v0.md` 4.9 (text 1 to 2,000 characters, no control characters, an identifier for `utterance_id`, a true/false `interrupt`); anything else gets `error` `bad_payload`, not fatal. The ack goes out when speech has started, or at once when the text waits behind speech that is already playing; a full queue (8), an interrupted wait or speech that does not start in 4 s gets `error` `busy`, an engine failure `error` `internal`. `interrupt: true` flushes the queue. Speech that runs far too long is cut off. Audio never runs on the link thread. Each utterance logs one line with its id and length (never the text) under the tag `EmbodiedLink`.

Debug builds also take test commands over adb, without touching the phone, through a broadcast receiver that requires `android.permission.DUMP` (held by the adb shell, preinstalled privileged apps, or an app granted it with `adb shell pm grant`; ordinary apps cannot get it on their own); results go to logcat under the tag `EmbodiedLink` and to the brain log:

```powershell
.\tools\phone-link.ps1 -Command connect            # add -UnreachableSeconds 30 to shorten the give-up time for a test
.\tools\phone-link.ps1 -Command echo -Count 1000 -Bytes 102400
.\tools\phone-link.ps1 -Command stop
adb logcat -s EmbodiedLink
```

Debug builds also take `POKE` (sends the poke event, same 3 s limit) and `SPEAK` (says "Embodied body speaker test." locally, no brain needed, then releases the engine): `adb shell am broadcast --allow-background-activity-starts -n com.jmd.embodied.body/.DebugCommandReceiver -a com.jmd.embodied.body.debug.POKE` (or `.SPEAK`). To try the whole path, run `python -m brain chat --body phone --fake-model`, press Connect (or `phone-link.ps1 -Command connect`), then Poke: the scripted model greets and the phone speaks it.

## Phone link

The laptop is a company device whose firewall blocks the phone, so the phone reaches the brain through `adb reverse tcp:8765 tcp:8765` over wireless debugging and connects to `127.0.0.1:8765`. A direct WiFi address would need a change to `body-android/app/src/main/res/xml/network_security_config.xml` (cleartext is allowed only to 127.0.0.1 and localhost), a rebuild, and `serve --allow-remote`; see `protocol/v0.md` open question 3 before sending camera or microphone data that way.

## Security notes

- The link token lives in your user profile, not in the repo, because other local accounts can modify files under `C:\jeffsource`: folders created under `C:\` inherit "Authenticated Users: Modify". The same applies to the code itself: another account on this laptop could edit the brain. If the laptop has other accounts, tighten the repo folder, for example `icacls C:\jeffsource\EmbodiedAgent /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"` (not run by the project; it changes folder permissions).
- Run the brain from a normal prompt, not "Run as administrator": files created elevated are owned by Administrators and the brain will refuse them.
- The link is authenticated but not encrypted (`protocol/v0.md` section 4.2): keep it on loopback and the adb link.

## Moving the project to another machine

See `docs/migration.md`.

## API key

See `API-KEY-SETUP.md` for how to get an Anthropic API key, where to put it, and the spend limits.
