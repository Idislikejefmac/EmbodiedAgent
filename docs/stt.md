# Local speech to text (card P2-04, STT half)

Status: built and measured on 15 Nov 2026. The segmenter (P2-03), the microphone path (P2-02) and the streaming
vendor engines are not built yet; this page covers the local engine, how it is installed and verified, and what it
costs in time on this laptop.

**Privacy: nothing leaves the laptop.** Audio and transcripts stay in this process. The brain makes no network call
for speech to text (the engine opens the model from a folder, sets `HF_HUB_OFFLINE=1`, and the egress guard would refuse
a socket anyway; `tests/test_stt_engine.py` and the `real_model` tests check that no outside connection is attempted).
The only program that talks to huggingface.co is `tools/fetch_stt_model.py`, run once by hand.

## What is installed

Python packages, into the project's `.venv` only (optional extra `stt` in `pyproject.toml`, every version pinned with `==`):

| Package | Version | Why |
| --- | --- | --- |
| faster-whisper | 1.2.1 | the engine |
| ctranslate2 | 4.8.5 | the inference runtime (CPU build used here) |
| onnxruntime | 1.32.0 | faster-whisper's bundled voice-activity model |
| tokenizers | 0.23.3 | text tokens |
| huggingface-hub | 2.4.0 | only `tools/fetch_stt_model.py` uses it |
| av | 19.0.2 | audio decoding library faster-whisper depends on (not used for our PCM) |
| numpy | 2.5.4 | audio arrays |
| click, filelock, flatbuffers, fsspec, hf-xet, protobuf, PyYAML, tqdm | 8.5.1, 4.2.9, 25.12.19, 2026.9.0, 1.8.0, 7.36.2, 6.0.3, 4.70.1 | pulled in by the above |

No torch and nothing GPU-specific was installed. Install on a fresh machine with `pip install -e ".[stt]"`.

Models (English only, per the plan), fetched into `data/models/stt/<name>/` (git-ignored):

| Model | Repo | Size | Pinned revision |
| --- | --- | --- | --- |
| base.en | Systran/faster-whisper-base.en | 145 MB | 3d3d5dee26484f91867d81cb899cfcf72b96be6c |
| small.en | Systran/faster-whisper-small.en | 484 MB | d1d751a5f8271d482d14ca55d9e2deeebbae577f |

## Fetch and verify the models (one time)

```powershell
.\.venv\Scripts\python tools\fetch_stt_model.py            # both models
.\.venv\Scripts\python tools\fetch_stt_model.py base.en    # one
.\.venv\Scripts\python tools\fetch_stt_model.py --list     # what is installed and whether it still verifies
.\.venv\Scripts\python tools\fetch_stt_model.py --force    # replace an installed model
```

Downloaded files are untrusted data. For each model the tool:

1. resolves the repository's current commit and **pins** it: every file is fetched at that exact revision, and the
   revision is written to the lock;
2. downloads into a **new, empty temporary folder** next to the target (same disk), never into the target;
3. checks the folder: only `config.json`, `model.bin`, `tokenizer.json`, `vocabulary.txt` (or `.json`) and
   `preprocessor_config.json` if present; no sub-folders, links, junctions or extra files; no `.pt`, `.pkl`, `.pickle`,
   second `.bin` or code files; each file non-empty and under `STT_MODEL_FILE_MAX_BYTES` (1 GiB); a `model.bin` that starts
   like a pickle or an archive is refused; the JSON files must parse;
4. compares every file with the hub's own record: `model.bin` by SHA-256 against the LFS hash, the small files by git's
   blob id (SHA-1 of `blob <size>`, NUL, bytes). It also computes SHA-256 for every file for the lock. Sizes must match the
   hub's listing (a truncated download is refused). A file the hub gives no hash for would be recorded as `first_seen`;
5. moves the folder into place atomically and rewrites `data/models/stt/lock.json`. An installed model that still
   verifies is never replaced without `--force`; one that no longer verifies is not silently replaced either (the tool says
   so and stops).

Nothing downloaded is ever executed, imported or unpickled. A failure at any step installs nothing and leaves no temporary
folder behind. The fetch on 15 Nov 2026 verified **every file of both models from hub metadata** (no `first_seen` entries):
`model.bin` by LFS SHA-256, the other three files by git blob id.

`lock.json` holds, per model: repo, revision, fetch date and, per file, size, SHA-256, how it was verified (`hub` or
`first_seen`) and the hub's id.

**How the brain uses the lock.** Before the first load the engine runs `verify_model` (`brain/voice/modelstore.py`): the
folder must hold exactly the locked files, each a plain file of the locked size and SHA-256. An unlisted, changed, extra,
missing, truncated or linked file refuses to load with a message saying which (`SttUnavailable`; `stt-check` exits 2).
Hashing is cached by file size and modification time in `data/models/stt/.verified.json`, so a start does not re-hash 600 MB.
The lock guards against damage and accident; someone who can already write to `data/models/stt/` could rewrite the lock too.

## Config

`config.toml` `[stt]` (checked at the config gate; an unknown key stops the brain with a did-you-mean message):

| Key | Default | Allowed |
| --- | --- | --- |
| `engine` | `"whisper-local"` | `whisper-local` |
| `model` | `"base.en"` | `base.en`, `small.en` |
| `device` | `"cpu"` | `cpu`, `auto` |
| `compute_type` | `"int8"` | `int8`, `float32` |
| `cpu_threads` | `0` (automatic) | 0 to 32 |
| `model_dir` | `"data/models/stt"` | a folder inside the config's own folder; an absolute path is accepted only inside the project or your user profile; no `..` |

A profile's config folder is `profiles/<name>/`, so its default `model_dir` is its own `profiles/<name>/data/models/stt`.
To share Jeff's downloaded models, set `model_dir` in the profile's `config.toml` to the full path of the project's
`data/models/stt` (allowed because it is inside the project).

## CPU, GPU and the driver note

The default and tested device is the **CPU with int8** (i7-11800H, 8 cores). The GPU (RTX 3080 Laptop, 8 GB) is **not
usable yet**: NVIDIA driver 511.65 supports CUDA 11.6 only, while current CTranslate2 and faster-whisper need CUDA 12 and
cuDNN 9. Updating the driver is Jeff's decision; nothing here changes it, and no CUDA package was installed.

`device = "auto"` is safe: if CTranslate2 sees no usable CUDA device it uses the CPU at once (that is what happens with the
present driver: `get_cuda_device_count()` is 0). If a device does show up, a real model load plus a one-second
transcription runs once in a **child process** with a timeout (`STT_CUDA_PROBE_TIMEOUT_S`), so a driver or library problem
can never take the brain down; only a passing self-test selects CUDA, and a CUDA load failure still falls back to the CPU.
`engine.device_used` and `stt-check` say which device ran and why.

## Check a recording

```powershell
.\.venv\Scripts\python -m brain stt-check some.wav                    # the model from config.toml
.\.venv\Scripts\python -m brain stt-check some.wav --model small.en
.\.venv\Scripts\python -m brain stt-check some.wav --profile mary
```

It takes a **16 kHz, mono, 16-bit PCM WAV** of at most 15 s and at most 1 MiB, validated with the standard `wave` module
(a header that promises more audio than the file holds, a wrong rate, channels or width, compression, an empty file or a
file that is not a WAV are refused with one line). It prints the model, device, model load time, audio length, decode time,
real-time factor and the text. Exit 0 done; 2 refused (bad file or arguments, model missing or changed); 1 failed. Never a
traceback. To make a test WAV without a person, Windows' built-in SAPI voice can write one (`SetOutputToWaveFile` with a
16 kHz 16-bit mono format); `tests/test_stt_real_model.py` does exactly that in a temporary folder.

## How the engine behaves

`brain/voice/stt.py`: `SttEngine` (`start(utterance_id)`, `feed(chunk)`, `await finish()`, `cancel()`, `close()`; optional
`on_partial` callback given to `start`), `SttResult` (text, language, duration, `decode_ms`, `queue_ms`, `total_ms`, `rtf`,
`avg_logprob`, `no_speech_prob`, segment count, `empty_reason`), the audio and transcript gates, and `LocalWhisperEngine`.
`FakeSttEngine` (scripted text, latency, errors, hang, partials) is in `tests/fakes/stt.py` and passes the same contract tests.

- **Audio gate.** Only `AudioChunk`s of 16 kHz, mono, `pcm_s16le`, whole samples, at most 5,120 bytes each, at most 2,000
  chunks and 480,000 bytes (15 s) per utterance. Anything else raises `SttBadAudio` and changes nothing (the over-long chunk
  is refused, the audio already held is kept). Fewer than 300 ms of audio, or digital silence (RMS under about -60 dBFS),
  returns a clean empty result without touching the model.
- **Worker.** One daemon worker thread decodes (never the event loop); at most two decodes are admitted (one running, one
  waiting), the third gets `SttBusy`. A hard timeout (`STT_DECODE_TIMEOUT_S`, 20 s, plus 60 s for the first load) and `cancel()`
  make `finish()` return at once with `SttTimeout` or `SttCancelled`. The native decode cannot be interrupted, so the worker is
  told to stop at the next segment and meanwhile new requests see `SttBusy`; the daemon thread cannot block process exit. An
  engine or library failure becomes `SttUnavailable`; the worker survives. A model that failed to load is not retried for 30 s.
- **Determinism.** `language="en"`, temperature 0, greedy decoding (`STT_BEAM_SIZE` 1), `condition_on_previous_text=False`,
  faster-whisper's built-in voice-activity filter on, `without_timestamps`.
- **Transcript hygiene.** NFKC, control, format (zero-width, bidi marks), surrogate and unassigned characters removed, all
  whitespace to single spaces, at most `STT_MAX_CHARS` (2,000) with a `truncated` flag. Silence hallucinations are dropped by
  Whisper's own rules (a segment with `no_speech_prob` over 0.6 and `avg_logprob` under -1.0, or compression ratio over 2.4),
  sound tags and music notes (`[MUSIC]`, `(laughs)`, notes) are removed, repetition loops are cut to two copies,
  "Thanks for watching", "Subtitles by the Amara.org community" and similar are always dropped, and a lone "Thank you." or
  "Bye." is dropped unless the model was confident (a clearly spoken "thank you" passes).
- **Events and health.** `speech.stt` (now live) carries utterance id, ms, rtf, chars, audio seconds, ok and error code,
  **never the words**; the words go to the prompt capture only when `[logging] full_prompts` is on (the caller's job). The
  `speech_vendors` health source shows local STT up after a transcript, degraded after a busy, timeout or bad-audio result, and
  down when the engine is unavailable (model missing or changed).

Limits (all in `brain/limits.py`, section A.6): `AUDIO_IN_RATE_HZ`, `UTTERANCE_MAX_S`, `UTTERANCE_MIN_MS`,
`UTTERANCE_MAX_BYTES`, `STT_CHUNKS_MAX`, `STT_MAX_CHARS` (the Appendix A name; the brief called it `TRANSCRIPT_MAX_CHARS`),
`STT_DECODE_TIMEOUT_S`, `STT_LOAD_TIMEOUT_S`, `STT_UNAVAILABLE_S`, `STT_PENDING_MAX`, `STT_BEAM_SIZE`, the hallucination
thresholds, `STT_CPU_THREADS_MAX`, the model-file caps and `STT_WAV_MAX_BYTES`. All provisional.

## Measured on this machine (CPU, int8, 15 Nov 2026)

Test speech: eight short English sentences made by Windows SAPI (Microsoft David), 16 kHz mono, 1.7 to 4.4 s, including the
names "Syd" and "Jeff", numbers, a command and a question. Each ran once cold and three more times warm, in one process
(`LocalWhisperEngine`, greedy, VAD filter on). **Synthetic clean speech is the best case**; a person at 1 m will be worse.

| | base.en | small.en |
| --- | --- | --- |
| model load (after file check, cached hashes) | 0.68 s | 1.16 s |
| first call overall (check + load + decode) | 1.29 s | 2.65 s |
| first decode right after load | 611 ms | 1,492 ms |
| warm decode, median of 24 | **512 ms** (range 439 to 551) | **1,555 ms** (1,485 to 1,642) |
| real-time factor, median (range) | 0.15 (0.12 to 0.28) | 0.46 (0.36 to 0.86) |
| 15.0 s utterance, decode | 627 ms (rtf 0.04) | 1,922 ms (rtf 0.13) |
| word error rate, 8 sentences, warm run | 4.3 % | 4.3 % |

Per sentence (warm decode ms, base / small): 439 to 551 / 1,485 to 1,642 whatever the length; the only errors for both models
were "Syd" heard as "Sid" (twice) and "asked" as "asks". Digits come out as digits ("555-1234", "23"); the WER helper in
`tests/wer.py` spells them out before comparing.

The decode time barely depends on the utterance length: Whisper's encoder always works on a 30 s window, so a 1.7 s and a
15 s utterance cost about the same, and short utterances have the worst real-time factor. `cpu_threads` made little difference
(base.en median 483 ms automatic, 440 ms with 8 threads; small.en 1,559 vs 1,329 ms with 8; more threads were not better);
the default stays automatic.

Silence (2 s of zeros) returns an empty result without decoding. White noise (2 s, standard deviation 3,000), quiet hiss and a
steady tone give an empty transcript with both models (the voice-activity filter drops them in about 7 ms; with it switched
off the model still produced no text, in about 0.5 s). Exactly 15 s decodes; 15 s plus one sample is refused with
`SttBadAudio`; a 16 s WAV is refused by `stt-check`.

**Does the CPU meet the plan's latency target? No.** The plan wants a final transcript within about 150 ms of the end of
speech and a median under 300 ms (T-P2-04-007). This engine decodes the whole utterance after it ends (no streaming yet), so
the delay after the end of speech is the decode time: about **0.5 s with base.en and about 1.6 s with small.en**, 1.7 times
and 5 times the 300 ms bar. This is before the segmenter's own end-of-speech wait (about 500 ms) and the rest of the turn.
What follows: `small.en` is too slow for conversation on this CPU and was not clearly more accurate on this set, so `base.en` is
the CPU default; even `base.en` misses the target by about 200 ms. A GPU would be expected to change this a lot (a Whisper
base model on an RTX-class GPU with CTranslate2 float16 typically decodes a short utterance in tens of milliseconds), but that
is an expectation, not a measurement here, and it needs the driver update first. Other routes to the target, none built: a
cloud engine (the plan's primary), a distilled model, or streaming partial results while the person is still speaking.
The accuracy part of the acceptance test (18 of 20 sentences at 1 m, Jeff's voice) is still to do.

## Not done yet

The VAD and segmenter (P2-03) and the microphone path (P2-02) are not built: `feed` takes chunks from whoever calls it.
Streaming partial text (the callback exists, the local engine never calls it), vocabulary hints so "Syd" is not heard as
"Sid" (faster-whisper accepts `initial_prompt` and `hotwords`; a bounded, sanitised hint list is a later step), the vendor
engine and its retry and spoken fallback, and GPU use.

## Tests

`tests/test_stt_engine.py` (contract tests for the fake and the local engine over a fake model, the boundary matrix, hygiene,
worker behaviour), `tests/test_stt_models.py` (lock verification, the fetch tool's checks on a fake hub, the WAV reader),
`tests/test_stt_cli_config.py` (the `[stt]` gate and `stt-check` in child processes with hostile arguments). They are fast and
offline. `tests/test_stt_real_model.py` loads the real models and is marked `real_model`: deselected by default like
`real_vendor`; run with `pytest tests\test_stt_real_model.py --run-real-model`. Chaos experiments: `P2-04.stt.engine`,
`.model_store`, `.wav_cli`, `.config` and `.real_engine` (`tools/chaos/cases/stt.py`; the last loads the real models only when
`EMBODIED_CHAOS_REAL_MODEL=1` and `chaos --allow-real-data` are given, which the `real_model` test does).
