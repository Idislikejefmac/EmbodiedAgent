# Profiles: a setup for each person

A profile is one person's own copy of the brain's settings and data, so a second person (for example Jeff's wife) can test the app with her own API key, her own spend limits, her own logs and her own link token, on the same laptop or another one, without sharing anything with Jeff's setup.

## What a profile is

| | Default (no `--profile`) | `--profile mary` |
|---|---|---|
| Settings | `config.toml` at the repo root | `profiles/mary/config.toml` |
| API keys | `ANTHROPIC_API_KEY` in the environment, else the root `.env` | **only** `profiles/mary/.env` |
| Data (logs, `state.json` and spend counters, `health.json`, crash reports, prompt capture) | `data/` | `profiles/mary/data/` |
| Link token | `%LOCALAPPDATA%\EmbodiedAgent\secrets` | `%LOCALAPPDATA%\EmbodiedAgent\profiles\mary\secrets` |
| Network port | 8765 (or whatever `config.toml` says) | its own, from 8766 up, written in its `config.toml` |

No `--profile` means exactly what it meant before profiles existed. A profile never falls back to the default's files: if something is missing in the profile, the brain says so and stops (exit code 2).

`--profile NAME` is accepted by every command (`info`, `serve`, `probe`, `chat`, `watch`, `health`, `demo-trace`, `body-check`, `pair`, `stt-check`), before or after the command. It is not used by `chaos` (the harness runs in its own sandbox) or by `tools/migrate.py` (which takes explicit paths; to move a profile, copy its folder by hand). `serve --supervised` passes it on to the child it restarts.

`profiles/` is git-ignored (it holds a `.env`), so nothing in it can be committed by accident; a test checks it.

## Names

A name is a lower-case letter, then lower-case letters, digits, `_` or `-`, at most 32 characters (`mary`, `jeff-2`, `test_3`). Refused with a message naming the rule: upper case, spaces, dots, slashes, drive letters, `..`, a leading digit or dash, anything longer than 32, control characters, Unicode look-alikes (only plain `a`-`z` is accepted), and these reserved names: `default`, `profiles`, `data`, `config`, and the Windows device names `con`, `prn`, `aux`, `nul`, `com1` to `com9`, `lpt1` to `lpt9`. The folder is built from the validated name alone and must be a direct child of `profiles/`; a folder link that points elsewhere is refused.

## Create a profile for another person

```powershell
.\.venv\Scripts\python -m brain profile create mary
```

This makes `profiles/mary/` with:

- `config.toml`: safe per-person defaults. Its own port (the next free one from 8766 up that no other profile or the root config uses; each person's brain needs its own port and its own `adb reverse tcp:PORT tcp:PORT`), `[caps]` session $1, daily $5, hard stop $10, 300 requests an hour, `[models] allow_live_calls = false`, `[logging] full_prompts = false`, egress limited to `api.anthropic.com`.
- `.env`: the key names with nothing after them.
- `README.txt`: where to put the key (no value in it).
- `data/`: empty. A profile's speech models default to its own `data/models/stt/`; to share Jeff's downloaded models instead, set `[stt] model_dir` in its `config.toml` to the full path of the project's `data/models/stt` (docs/stt.md).

It never overwrites: if the profile exists, it says so and changes nothing. If it fails part-way (full disk, a file where a folder belongs) it removes only the half-made folder it just created.

```powershell
.\.venv\Scripts\python -m brain profile list          # name, port, live calls, key present or missing, capture, last used
.\.venv\Scripts\python -m brain profile show mary     # the effective config, no secrets
.\.venv\Scripts\python -m brain info --profile mary   # also says which file the key would come from
```

`list` and `info` show key names as present or missing, never a value.

## Getting her own API key

She needs a key that bills to a spend limit that is hers, not Jeff's. Two ways:

1. **A separate workspace in Jeff's Individual Org** (Console, Settings, Workspaces): create a workspace for her, set that workspace's monthly spend limit, and create an API key inside it. The key can only spend in that workspace, and the limit stops it. Jeff pays, but with a cap he chooses.
2. **Her own Console account** with her own card or credit: she creates the key and sets her own limit. Nothing of hers is on Jeff's bill.

Either way, the Console limit is the real stop. The `[caps]` in her `config.toml` are the brain's own stop, in addition. Keep the Console limit at or above the brain's caps, never below, so the brain's message comes first. See `docs/cost-estimate.md` for what a session costs.

## Where the key goes

Open `profiles/mary/.env` and put the key after its name on the same line, no spaces or quotes:

```
ANTHROPIC_API_KEY=her-key-here
```

That file is the only place a profile reads keys from. An `ANTHROPIC_API_KEY` set in the environment, or in Jeff's root `.env`, is **ignored on purpose**, so that nobody can spend another person's credit by accident. `info --profile mary` shows `key ANTHROPIC_API_KEY: missing` until the key is in that file, and names the file it looked in. Never paste a key into a chat, a document or `config.toml`.

To allow real (paid) calls she must also set `allow_live_calls = true` in her `config.toml` `[models]`; until then `chat --live` refuses.

## Running it

```powershell
.\.venv\Scripts\python -m brain chat --profile mary            # the scripted model: free, no key
.\.venv\Scripts\python -m brain chat --profile mary --live     # the real model: her key, her caps
.\.venv\Scripts\python -m brain serve --profile mary           # her brain, on her port
.\.venv\Scripts\python -m brain watch --profile mary --follow  # her logs
```

Two brains can run at the same time because their ports differ. For a phone, `adb reverse tcp:8766 tcp:8766` (her port) and pair with `pair --profile mary`; her token differs from Jeff's, so her phone cannot connect to Jeff's brain or the other way round.

## Prompt capture is off by default, and why

With `full_prompts = true` the brain saves every prompt and reply, including what the microphone heard, in `data/capture`. For Jeff's own development that is useful; for another person it is a recording of her conversations that Jeff (who owns the laptop) could read. So a profile starts with it **off**, and she turns it on herself in her `config.toml` `[logging]` only if she wants it for troubleshooting. `profile list` shows which profiles have it on.

## What is shared and what is not

Not shared: the settings, the keys, the spend counters (`state.json`: session, day and total are per profile), the events and logs, the health file, crash reports, the prompt capture, the link token and the port.

Shared: the program code and the files that come with it (the price table `brain/agent/prices.json`, the system prompt, the tool list), the machine's network and the phone's `adb`, and the Windows user account that runs it.

## The honest limits

A profile is **separation of configuration and data on one machine, not a security boundary between Windows users.** Anyone who can read the files of the Windows account that runs the brain can read every profile's `.env` and data. The key isolation rule protects against accidents (the wrong key, the wrong spend), not against a person who can open the folder. For real privacy between two people, give each their own Windows account (or their own machine); the profile token folder is under the current user's `%LOCALAPPDATA%` and the existing token-store checks still apply to it. Prompt capture, if turned on, is readable by whoever can read that folder.

## Removing a profile

There is deliberately no delete command: deleting a person's data is done by hand. Close the brain, delete the folder `profiles\mary`, and delete `%LOCALAPPDATA%\EmbodiedAgent\profiles\mary` (the token). Also revoke her key in the Console if she is finished with it. The same README is written inside each profile folder.

## For developers

- Code: `brain/profiles.py` (names, create, list, show), `Profile` and `ProfileError` in `brain/config.py` (the gate hands every command a `Config` whose `.profile` says whose setup it is), `profile_secrets_dir` in `brain/transport/auth.py`, `split_profile_arg` in `brain/__main__.py`. Everything that has a location (the event files, `state.json`, the spend guard, the capture, `health.json`, crash reports) already takes it from `config.paths.data_dir`; the token store takes it from `secrets_dir(config)`; the model key comes from `config.profile.api_key(...)` in `brain/chat.py`.
- Limits (provisional, `brain/limits.py` A.5): `PROFILE_NAME_MAX_CHARS`, `PROFILE_PORT_FIRST`, `PROFILES_MAX`.
- Tests: `tests/test_profiles.py` (names, hostile names, scaffold, the canary-key isolation proofs, separation of events, state, spend, capture and tokens, two brains at once, the git-ignore rule), `tests/test_profiles_cli.py` (child processes: hostile names, missing, corrupt and odd profiles), chaos experiments `SYS.profiles.name_gate` and `SYS.profiles.isolation` (`tools/chaos/cases/profiles.py`).
