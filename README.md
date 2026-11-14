# Embodied Agent

A trial of a Claude agent with a body: first an Android phone, later possibly a wheeled robot. The full plan is in `EmbodiedAgent-Plan-and-Implementation-Order.md`.

## Layout

| Folder | What |
| --- | --- |
| `body-android/` | The Android app (Kotlin). The phone's eyes, ears, mouth and pose sensor |
| `brain/` | The Python brain on the laptop. Body-agnostic; only `brain/body/phone.py` may mention the phone. It reaches its body through the `Body` interface (`docs/adding_a_body.md`) |
| `protocol/` | The body-to-brain WebSocket protocol |
| `tools/` | Command-line tools (for example data migration) |
| `docs/` | Design notes, gate records, privacy and migration docs |
| `data/` | Private runtime data. Git-ignored and created locally |

## Setup (Windows, PowerShell)

```powershell
py -3.13 -m venv .venv    # Python 3.12 or newer
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env     # then fill in keys; .env is git-ignored
```

## Run the brain tests

```powershell
.\.venv\Scripts\python -m pytest -q
```

The protocol tests check the schema, every example in `protocol/v0.md`, the binary frame layout, the server against a Python test body, and that the body's Kotlin allowlist matches the spec. The body tests run one shared contract (`tests/body_contract.py`) against the simulated body and against the phone body over loopback (no phone needed). The tool tests check the agent's tool schemas and that every request outside the allowlist is refused and logged. The whole suite runs under the egress guard, so no test can reach the network.

Type checking (mypy, strict, over `brain/body`, `brain/agent` and `brain/egress.py`; also run by `tests/test_typing.py`):

```powershell
.\.venv\Scripts\python -m mypy
```

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

Install on a paired phone with `adb install -r app\build\outputs\apk\debug\app-debug.apk`. Run the Kotlin unit tests (binary frame format, authentication vectors, clock estimator, backpressure, validation) with `.\gradlew.bat testDebugUnitTest`.

The app opens on the brain-link screen: a `host:port` setting (default `127.0.0.1:8765`), a link-state indicator (connected, retrying, stopped, not paired), Connect and Stop buttons, an echo-test button and an "ARCore check" button for the P0-02 checker. Nothing runs until Connect is pressed. The link then runs in a foreground service with a notification (with a Stop action), so it stays up with the screen locked and reconnects by itself when the brain restarts. It stops itself after 3 minutes without a stable link (one that stayed up 10 s), when the app is swiped away, when the brain shuts down and does not return within a minute, or when another body replaces it; authentication failures are retried within those 3 minutes; it holds a CPU wake lock only while connected. The screen is kept on only during an echo test.

Debug builds also take test commands over adb, without touching the phone, through a broadcast receiver that requires `android.permission.DUMP` (held by the adb shell, preinstalled privileged apps, or an app granted it with `adb shell pm grant`; ordinary apps cannot get it on their own); results go to logcat under the tag `EmbodiedLink` and to the brain log:

```powershell
.\tools\phone-link.ps1 -Command connect            # add -UnreachableSeconds 30 to shorten the give-up time for a test
.\tools\phone-link.ps1 -Command echo -Count 1000 -Bytes 102400
.\tools\phone-link.ps1 -Command stop
adb logcat -s EmbodiedLink
```

## Phone link

The laptop is a company device whose firewall blocks the phone, so the phone reaches the brain through `adb reverse tcp:8765 tcp:8765` over wireless debugging and connects to `127.0.0.1:8765`. A direct WiFi address would need a change to `body-android/app/src/main/res/xml/network_security_config.xml` (cleartext is allowed only to 127.0.0.1 and localhost), a rebuild, and `serve --allow-remote`; see `protocol/v0.md` open question 3 before sending camera or microphone data that way.

## Security notes

- The link token lives in your user profile, not in the repo, because other local accounts can modify files under `C:\jeffsource`: folders created under `C:\` inherit "Authenticated Users: Modify". The same applies to the code itself: another account on this laptop could edit the brain. If the laptop has other accounts, tighten the repo folder, for example `icacls C:\jeffsource\EmbodiedAgent /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"` (not run by the project; it changes folder permissions).
- Run the brain from a normal prompt, not "Run as administrator": files created elevated are owned by Administrators and the brain will refuse them.
- The link is authenticated but not encrypted (`protocol/v0.md` section 4.2): keep it on loopback and the adb link.

## Moving the project to another machine

See `docs/migration.md`.
