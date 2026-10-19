# FileNap

FileNap asks a local Gemma model to plan destinations for a small pile of files. It shows a
destination, a reason and a source quote for each suggestion. You select the proposed moves
to apply. A local journal records the moves so an interrupted batch can be resumed and
unchanged files can be put back.

The project was started during the DEV Hacktoberfest 2026 Week 3 build window. It was built
with Codex assistance. The included example documents are fictional.

[Watch the recorded demo](https://HyunsikParker.github.io/filenap-hf26/), or open the
[local demo page](docs/index.html). The 42.6-second edit removes most of an 89.6-second
model wait and slows the review steps. A full 111.3-second recording preserves the timing.

## Run

Requires Python 3.11 or newer, Ollama, and a cached local model. The file-operation adapter
supports macOS and Linux; the current validation record identifies which systems were tested.
There are no Python package dependencies.

Start your local Ollama runtime with cloud features disabled:

```sh
OLLAMA_NO_CLOUD=1 ollama serve
```

In a second terminal, from this repository:

```sh
python3 -m filenap --demo
```

Open the printed `http://127.0.0.1:18731` address. Select example files, make a plan, review
the source quotes, then approve the moves you want. Move history contains the undo record.

To use an existing local runtime on a different port:

```sh
python3 -m filenap --demo --ollama-url http://127.0.0.1:11446
```

For a folder you choose:

```sh
python3 -m filenap --folder /absolute/path/to/a/staged-folder
```

FileNap writes a private `.filenap` journal and a `Sorted` directory inside the selected
folder. It does not recursively organize existing subfolders. Start with a copy of a folder
while checking whether the suggestions suit your files. Stop the app with Ctrl-C.

## What the model does

Gemma receives the selected file names, short text previews, your folder goal and the allowed
destination names. It returns one JSON decision per file: propose a move, keep it here, or
leave it for review. The application validates file IDs and destination names. A proposed
move without an exact quote from the supplied text is held for review.

The model cannot call a shell, delete a file, invent another source path, or apply its own
plan. The application accepts only loopback HTTP model endpoints and requires a cached
local model. It does not download weights, request API keys, or fall back to a hosted model.
Requests do not use environment proxies or follow HTTP redirects to another endpoint.

Classification can still be wrong. A matching quote is evidence of what was read, not proof
that the suggested folder is correct.

## Moves and recovery

- File bytes and metadata are checked against the saved plan before approval is applied.
- Each move uses the system's atomic rename operation with destination replacement disabled.
  A name collision preserves both files.
- Intent is committed to SQLite before the move. On reopening the same folder, an unfinished
  intent is reconciled from file hashes. Resume applies the remaining approved moves.
- Undo refuses to overwrite a recreated original name or restore a moved file that has been
  edited. The conflict stays visible and can be retried after it is resolved.

The application does not lock out other programs editing the folder. Avoid concurrent
changes while applying moves. It is not a security boundary against another process running
with your user permissions. Cross-filesystem moves are not supported by the atomic adapter.

Hidden files, symlinks, hard links and special files are excluded. Text previews are limited
to UTF-8 content. The limits are 8 MiB per file, 128 MiB per scan, 500 scanned files and 16 files
per model plan. Binary or unclear content remains available for manual review.

## Local model used in development

The recorded local run used Ollama **0.35.1**, Gemma4 **11.9B**, **Q4_K_M**, with
`think:false`. The attached model license is Apache 2.0.

```text
Local tag:      gemma4:12b-it-q4_K_M
Manifest:      4eb23ef187e2c5462566d6a1d3bbbc2f1346d0b4327cbb66d58fffbcc9b2b05c
Model weights: 1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606
License:       0d542e0c8804e39aa7f37eb00da5a762149dc682d7829451287e11b938e94594
```

The public Ollama tag had different model bytes when checked on October 20, 2026. A matching
tag name is not a byte-for-byte reproduction of this cached artifact. No weights are included
in this repository. Other local models can be selected with `--model`, but their behavior has
not been established by the Gemma run.

Sources: [Gemma4 on Ollama](https://ollama.com/library/gemma4:12b-it-q4_K_M),
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs),
[Ollama local/cloud configuration](https://docs.ollama.com/faq).

## Tests

The final 19-test suite passed on macOS with Python 3.13.12 and on Linux/WSL2 with Python
3.12.3. The tested source hashes matched across the two systems.

```sh
python3 -m unittest discover -s tests -v
```

The tests use new temporary files and explicit test doubles. They cover hash changes,
destination collisions, missing or invented model evidence, symlinks, crash reconciliation,
undo conflicts, loopback endpoint checks and the HTTP origin/session boundary. They do not
stand in for a live model run or establish classification accuracy on real personal files.

In the recorded live Gemma case, 12 fictional files produced nine moves, one keep decision
and two review decisions. Nine approved files were moved and then undone. A filesystem
readback confirmed that all 12 original SHA-256 hashes were restored. Planning took 89.6
seconds on an Apple M4 Mac with 32 GiB of memory. This is one fixture, not a benchmark.

## License

The application code is MIT licensed. Model weights and the Ollama runtime retain their
own licenses and are not redistributed here.
