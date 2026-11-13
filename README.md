# Embodied Agent

A trial of a Claude agent with a body: first an Android phone, later possibly a wheeled robot. The full plan is in `EmbodiedAgent-Plan-and-Implementation-Order.md`.

## Layout

| Folder | What |
| --- | --- |
| `body-android/` | The Android app (Kotlin). The phone's eyes, ears, mouth and pose sensor |
| `brain/` | The Python brain on the laptop. Body-agnostic; only `brain/body/phone.py` may mention the phone |
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
.\.venv\Scripts\python -m pytest
```

## Run the brain

```powershell
.\.venv\Scripts\python -m brain
```

Prints the version, loads `config.toml`, and says which API keys are present or missing (never their values).

## Build the Android app

First create `body-android/local.properties` containing `sdk.dir=C\:/Users/<you>/AppData/Local/Android/Sdk` (it is git-ignored). The build uses the JDK bundled with Android Studio, so adjust the path below if Studio is installed elsewhere.

```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
cd body-android
.\gradlew.bat assembleDebug
```

Install on a paired phone with `adb install -r app\build\outputs\apk\debug\app-debug.apk`.

## Phone link

The laptop is a company device whose firewall blocks the phone, so the phone reaches the brain through `adb reverse tcp:8765 tcp:8765` over wireless debugging and connects to `127.0.0.1:8765`.

## Moving the project to another machine

See `docs/migration.md`.
