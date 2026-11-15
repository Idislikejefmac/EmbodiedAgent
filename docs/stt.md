# Local speech to text (card P2-04, STT half)

Status: built and measured on 15 Nov 2026; the GPU path added the same day. The segmenter (P2-03), the microphone path (P2-02) and the streaming
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

No torch. Install on a fresh machine with `pip install -e ".[stt]"`; for an NVIDIA GPU add the optional extra `stt-gpu` (next table).

| GPU package (extra `stt-gpu`) | Version | Why |
| --- | --- | --- |
| nvidia-cublas-cu12 | 12.9.2.10 | cuBLAS, matrix multiplication on the GPU |
| nvidia-cudnn-cu12 | 9.28.1.4 | cuDNN 9, which this ctranslate2 needs |
| nvidia-cuda-nvrtc-cu12 | 12.9.86 | the CUDA runtime compiler (pulled in by cuDNN; pinned so the install is repeatable) |

About 1.6 GB of downloads, into the project's `.venv` only (`pip install -e ".[stt,stt-gpu]"`). They are the only things the GPU needs besides a **CUDA 12 capable NVIDIA driver (525 or newer; this machine has 621.24)**: no CUDA toolkit, no torch. The engine finds their DLLs by itself (see "The GPU").

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
| `device` | `"cpu"` | `cpu`, `auto`, `cuda` |
| `compute_type` | `"int8"` | `int8`, `float32`, `int8_float16`, `float16` |
| `cpu_threads` | `0` (automatic) | 0 to 32 |
| `model_dir` | `"data/models/stt"` | a folder inside the config's own folder; an absolute path is accepted only inside the project or your user profile; no `..` |

A misspelt value gets a did-you-mean (`device = "cud"` says `did you mean 'cuda'?`). `int8_float16` and `float16` are GPU types: on the CPU they are replaced by `int8` (see the table under "The GPU"). This machine's `config.toml` has `device = "auto"` and `model = "small.en"`; the file says that `small.en` is only sensible with a GPU (on a CPU-only machine use `base.en`).

A profile's config folder is `profiles/<name>/`, so its default `model_dir` is its own `profiles/<name>/data/models/stt`.
To share Jeff's downloaded models, set `model_dir` in the profile's `config.toml` to the full path of the project's
`data/models/stt` (allowed because it is inside the project).

## The GPU

Status: built and measured on 15 Nov 2026 (RTX 3080 Laptop, 8 GB, driver 621.24, ctranslate2 4.8.5, cuBLAS 12.9, cuDNN 9.28).

**Finding the CUDA libraries.** The pip wheels put their DLLs in `.venv\Lib\site-packages\nvidia\<name>\bin\`. Windows will not find them by itself, so before any CUDA use `register_cuda_dlls()` (`brain/voice/stt.py`) calls `os.add_dll_directory` for each of those folders that exists and puts them first on `PATH`. It looks only under the running interpreter's own site-packages (never at a path from the config or the environment), skips a link that leads out of there, does nothing off Windows, runs once per process (idempotent) and never raises. The self-test child process is handed the same folders and registers them itself.

**Device resolution** (`[stt] device`, then the one-time self-test):

| `device` | CUDA device and self-test | What runs | Health and event |
| --- | --- | --- | --- |
| `cpu` | not looked at | CPU | nothing GPU-related |
| `auto` | usable and self-test passes | GPU | `GPU int8_float16` in the reason; `speech.stt` phases `device`, `warming`, `warm` |
| `auto` | none, or the self-test fails or times out | CPU | `degraded`, reason `GPU unusable: <short reason>, using CPU` (the reason then rides along on every transcript event) |
| `auto` | self-test passes but the real load fails | CPU | the same reason, `the load failed (<error type>)` |
| `cuda` | usable and self-test passes | GPU | as `auto` |
| `cuda` | not usable | **nothing**: `SttUnavailable("[stt] device = cuda, but the GPU is not usable: <reason>")` | `down`, with the reason; never quietly the CPU |

**The self-test** runs once per process (cached per model folder and compute type): a cheap check first (`ctranslate2.get_cuda_device_count()`), and only if a device shows up a real model load plus a one-second transcription in a **child process** (`python -I -c ...`, no stdin, `STT_CUDA_PROBE_TIMEOUT_S` = 90 s), so a driver problem, a missing DLL or even a native crash can never take the brain down. The child prints one short failure line that becomes the reason (cleaned, cut to `STT_GPU_REASON_MAX_CHARS`); a timeout or a crash is reported as such. The brain process itself makes no network call (`HF_HUB_OFFLINE=1`).

**Compute type** (`resolve_compute_type`), after the device is known:

| device resolved to | `compute_type` asked | Used | Why |
| --- | --- | --- | --- |
| GPU | `int8` (the default) | `int8_float16` | measured fastest on this GPU; the reason says `int8 became int8_float16 on the GPU` |
| GPU | `int8_float16`, `float16`, `float32` | as asked | |
| CPU | `int8`, `float32` | as asked | |
| CPU | `float16`, `int8_float16` | `int8` | float16 does not exist on this CPU; the reason says so |

`cpu_threads` is passed to the model only on the CPU (the GPU load always gets 0).

**Warm-up.** The first CUDA decode in a fresh process pays the CUDA, cuBLAS and cuDNN start-up (about 7.3 s for base.en in the first measurement, 0.9 s for small.en; later runs, with the DLLs in the system's file cache, are shorter: see below). `warm()` loads the model if needed and decodes one second of very quiet synthetic noise (`STT_WARM_AUDIO_S`, `STT_WARM_LEVEL`) twice: once with the voice-activity filter off so the model really runs, once with it on so its model is loaded too (without the second pass the first real decode still took 460 ms on base.en). On the GPU the first utterance does this by itself, in the worker thread, so the event loop is never blocked; calling `await engine.warm_async()` at start-up (as `stt-check` does) moves the cost to before the first word. It is bounded by `STT_WARM_TIMEOUT_S` (45 s; then the engine goes on, un-warmed), runs once, returns the milliseconds or `None`, and **never raises**. Events: `speech.stt` with `phase` = `warming`, then `warm` (with `warm_ms`). Health source `speech_vendors`: `unknown` with reason `warming up the speech model on cuda`, then `up` (`warm on cuda after N ms`). On the CPU there is no automatic warm-up (it would only add about 0.5 s to the first utterance); `warm()` still works there when asked. The first utterance's time limit includes the self-test and the warm-up (`STT_CUDA_PROBE_TIMEOUT_S` plus `STT_WARM_TIMEOUT_S`); later ones get only `STT_DECODE_TIMEOUT_S`.

**When the GPU fails while the brain is running.** A CUDA out-of-memory error, or any other CUDA or driver error (cuBLAS, cuDNN, an illegal memory access, a driver reset) raised by a decode on the GPU, whether by the call or while the segments are consumed, fails **that decode only** with a named error: `SttGpuOutOfMemory` or `SttGpuError` (both `SttError`; short and safe to show; the raw library text never reaches the message). The engine then drops the GPU model and does **one** automatic fall back: every later load is on the CPU (compute type resolved for the CPU, `cpu_threads` honoured), with a `speech.stt` event `phase = fallback` (WARNING), a `degraded` health reason `GPU failed: <what>, using CPU` that stays while the process lives, and the new device in every later event. There is no second fall back and no retry of the GPU in this process (a restart tries again). An error that does not look like a GPU error (a plain `ValueError`, say) is not a reason to leave the GPU: it fails that decode as before (`SttUnavailable`). The same fall back happens if the GPU fails during the warm-up, before the first utterance, and then the caller sees nothing at all. It applies to `device = "cuda"` too: once the engine has started, a GPU failure never stops it from working.

**Checking it.** `python -m brain stt-check FILE.wav [--model M] [--device cpu|auto|cuda] [--compute-type T]` first loads and warms up, then decodes, and prints for example:

```
model small.en, device cuda int8_float16 (CUDA self-test passed; int8 became int8_float16 on the GPU)
model load 1829 ms, GPU self-test 2730 ms, warm-up 567 ms
audio 2.58 s, decode 141 ms, real-time factor 0.055
text: What time is it, Jeff?
```

(`--device cuda` exits 2 with the reason when the GPU cannot be used.) The real-GPU tests are `tests/test_stt_gpu_real.py` (`--run-real-model`; they skip when no CUDA device is present); the fake-layer tests are `tests/test_stt_gpu.py`.

### Measured: CPU against GPU (15 Nov 2026)

Six synthetic sentences made by Windows SAPI (Microsoft David), 16 kHz mono, 1.7 to 3.8 s, through the real `LocalWhisperEngine` (greedy, voice-activity filter on), each configuration in a fresh process: model load, `warm()`, then every sentence four times; the first sentence's decode right after the warm-up, and median, minimum and maximum of the last three rounds (18 decodes). Word error rate is the mean over the six sentences of the second round, digits spelled out (`tests/wer.py`).

| Device, model | Load | GPU self-test | Warm-up | First decode after warm-up | Median | Min to max | WER |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CPU base.en (int8) | 0.85 s | none | 0.79 s | 707 ms | **680 ms** | 639 to 767 ms | 2.8 % |
| CPU small.en (int8) | 1.58 s | none | 2.06 s | 2,070 ms | **2,075 ms** | 1,953 to 2,246 ms | 2.8 % |
| GPU base.en (int8_float16) | 0.89 s | 1.9 s | 0.51 s | 92 ms | **74 ms** | 51 to 104 ms | 2.8 % |
| GPU small.en (int8_float16) | 1.83 s | 2.7 s | 0.57 s | 155 ms | **141 ms** | 109 to 195 ms | 2.8 % |

The only error in every configuration is "Syd" heard as "Sid". GPU memory (`nvidia-smi`, whole card, before the model and with it loaded and decoding): 549 MiB to 836 MiB with base.en (+287 MiB) and to 1,060 MiB with small.en (+511 MiB), of 8,192 MiB. The one-time self-test child needs about the same again for 2 to 3 s while it runs, in another process, before the engine loads its own copy. A first, cold measurement with a plain script (no voice-activity warm-up, DLLs not yet in the system's file cache) gave 165 ms and 247 ms medians and 7.3 s for the very first decode of base.en; the engine's numbers above are what to expect.

**The plan's target** (final transcript about 150 ms after the end of speech, median under 300 ms; T-P2-04-007), counting the decode time because the engine decodes the whole utterance after it ends: **CPU base.en 680 ms: no. CPU small.en 2,075 ms: no. GPU base.en 74 ms: yes, inside the 150 ms goal as well. GPU small.en 141 ms: yes, just inside the 150 ms goal** (its slowest decode was 195 ms). All of this is before the segmenter's own end-of-speech wait of about 500 ms and the rest of the turn. So `small.en` is the choice on a machine with a GPU and `base.en` on one without (and even then it misses the target on this CPU).

## Check a recording

```powershell
.\.venv\Scripts\python -m brain stt-check some.wav                    # the model from config.toml
.\.venv\Scripts\python -m brain stt-check some.wav --model small.en
.\.venv\Scripts\python -m brain stt-check some.wav --profile mary
```

It takes a **16 kHz, mono, 16-bit PCM WAV** of at most 15 s and at most 1 MiB, validated with the standard `wave` module
(a header that promises more audio than the file holds, a wrong rate, channels or width, compression, an empty file or a
file that is not a WAV are refused with one line). It prints the model, device and compute type, model load time (and the GPU self-test and warm-up times), audio length, decode time,
real-time factor and the text; `--device` and `--compute-type` override the config for one run. Exit 0 done; 2 refused (bad file or arguments, model missing or changed); 1 failed. Never a
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
  down when the engine is unavailable (model missing or changed). With the GPU the source also shows `warming` then `up`, `degraded` with `GPU unusable: ..., using CPU` or `GPU failed: ..., using CPU`, and `down` when `device = "cuda"` cannot be satisfied (see "The GPU").

Limits (all in `brain/limits.py`, section A.6): `AUDIO_IN_RATE_HZ`, `UTTERANCE_MAX_S`, `UTTERANCE_MIN_MS`,
`UTTERANCE_MAX_BYTES`, `STT_CHUNKS_MAX`, `STT_MAX_CHARS` (the Appendix A name; the brief called it `TRANSCRIPT_MAX_CHARS`),
`STT_DECODE_TIMEOUT_S`, `STT_LOAD_TIMEOUT_S`, `STT_UNAVAILABLE_S`, `STT_PENDING_MAX`, `STT_BEAM_SIZE`, the hallucination
thresholds, `STT_CPU_THREADS_MAX`, the model-file caps, `STT_WAV_MAX_BYTES`, and for the GPU `STT_CUDA_PROBE_TIMEOUT_S`, `STT_WARM_TIMEOUT_S`, `STT_WARM_AUDIO_S`, `STT_WARM_LEVEL` and `STT_GPU_REASON_MAX_CHARS`. All provisional.

## Measured on this machine (CPU, int8, 15 Nov 2026, first measurement)

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
the CPU default; even `base.en` misses the target by about 200 ms (the GPU section has the later numbers). The GPU does change it a lot (measured: 74 ms with base.en, 141 ms with small.en; see "The GPU"). Other routes to the target, none built: a
cloud engine (the plan's primary), a distilled model, or streaming partial results while the person is still speaking.
The accuracy part of the acceptance test (18 of 20 sentences at 1 m, Jeff's voice) is still to do.

## Not done yet

The VAD and segmenter (P2-03) and the microphone path (P2-02) are not built: `feed` takes chunks from whoever calls it.
Streaming partial text (the callback exists, the local engine never calls it), vocabulary hints so "Syd" is not heard as
"Sid" (faster-whisper accepts `initial_prompt` and `hotwords`; a bounded, sanitised hint list is a later step), the vendor
engine and its retry and spoken fallback.

## Tests

`tests/test_stt_engine.py` (contract tests for the fake and the local engine over a fake model, the boundary matrix, hygiene,
worker behaviour), `tests/test_stt_models.py` (lock verification, the fetch tool's checks on a fake hub, the WAV reader),
`tests/test_stt_cli_config.py` (the `[stt]` gate and `stt-check` in child processes with hostile arguments). `tests/test_stt_gpu.py` (the GPU half over a fake CUDA layer: DLL folder search and registration, the self-test child, device and compute-type resolution, the warm-up, GPU out-of-memory and CUDA errors, the fall back, health states). They are fast and
offline. `tests/test_stt_real_model.py` loads the real models and is marked `real_model`: deselected by default like
`real_vendor`; run with `pytest tests\test_stt_real_model.py --run-real-model`. `tests/test_stt_gpu_real.py` does the same on the GPU (both models, the text, a second decode under 600 ms, the warm-up removing the first-decode penalty, `stt-check --device cuda`) and skips when no CUDA device is present. Chaos experiments: `P2-04.stt.engine`,
`.model_store`, `.wav_cli`, `.config`, `.gpu` and `.real_engine` (`tools/chaos/cases/stt.py`; the last loads the real models only when
`EMBODIED_CHAOS_REAL_MODEL=1` and `chaos --allow-real-data` are given, which the `real_model` test does).
