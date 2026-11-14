# Shared hostile corpus

`v0-hostile.json` holds hostile and valid protocol v0 frames, each with the verdict (accept or reject and the
error code) produced by running the Python validator (`brain.transport.protocol`). It is generated, never edited:

    .\.venv\Scripts\python tools\gen_hostile_corpus.py          # regenerate
    .\.venv\Scripts\python tools\gen_hostile_corpus.py --check  # fail if the committed file is stale

Text cases carry the exact wire string; binary cases carry the frame as lowercase hex. The Kotlin test
`body-android/app/src/test/java/com/jmd/embodied/body/HostileCorpusTest.kt` replays the same file and lists every
known Python/Kotlin divergence.
