# plex-audio-audit

A two-stage Plex library audio tool.

**Stage 1 (`audit.py`)** is **read-only** — it walks your media libraries,
inspects each video file's audio tracks via `ffprobe`, classifies what (if any)
action each file needs, and writes a per-library summary plus a CSV worklist. No
media files are modified.

**Stage 2 (`remediate.py`)** acts on that worklist, fixing the fixable files with
metadata-only `mkvpropedit` edits (no re-encoding). See
[Stage 2 — reflag remediation](#stage-2--reflag-remediation) below.

## What Stage 1 does

For every file under each configured library root with a scanned extension
(`.mkv` / `.mp4` / `.m4v` by default), the audit reads each audio track's
codec, channel count, language tag, title, and default-disposition flag,
then assigns one classification per file:

| Class      | Rule |
|------------|------|
| `OK`       | An English audio track is flagged default **and** its codec is in `ok_codecs` (default: `aac`, `ac3`, `eac3`, `dts`). |
| `REFLAG`   | An English audio track exists but **none** are flagged default — fixable with a metadata-only change at Stage 2. Also catches tracks where the language tag is missing/`und` but the title contains "English". |
| `ENCODE`   | No English audio track at all, **or** the default English track uses a codec that needs transcoding for direct play (default: `truehd`, `dts-hd`, `flac`, `mlp`, `opus`). |
| `SKIP-MP4` | Container is `.mp4` / `.m4v`. Track metadata is still captured in the CSV for inspection, but Stage 2 won't touch these files. |
| `SKIP`     | `ffprobe` failed, timed out, or found no audio streams. The reason is recorded in the CSV. |

## Run

Build the image once:

```sh
docker build -t plex-audio-audit .
```

Then point it at your libraries (mount them **read-only**) and a writable
reports directory:

```sh
docker run --rm \
  -v /Volumes/Plex/Movies:/media/movies:ro \
  -v /Volumes/Plex/TV:/media/tv:ro \
  -v $(pwd)/reports:/reports \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  plex-audio-audit
```

The container expects libraries at `/media/movies` and `/media/tv` and writes
reports to `/reports`. Adjust the host paths on the left side of each `-v` to
match where your media actually lives.

## Configure

Edit `config.yaml`:

- `library_roots` — list of `{name, path}` pairs. Paths are the
  **in-container** paths (default `/media/movies`, `/media/tv`); mount your
  host paths to those locations in `docker run`.
- `scan_extensions` — case-insensitive file extensions to scan.
- `reports_dir` — where `worklist.csv` and `summary.txt` are written
  (default `/reports`).
- `classification.target_language` / `ok_codecs` / `encode_codecs` — the rules
  defined in the table above. Tweak if your client preferences differ.

## Outputs

`reports/worklist.csv` — one row per scanned file, columns:

```
library, relative_path, classification, reason, track_count,
codecs, channels, languages, titles, defaults
```

The track-level fields (`codecs` through `defaults`) are `;`-separated in
track-index order, so a single row covers every audio track in the file.

`reports/summary.txt` — per-library counts by classification plus a grand
total. The same summary is also printed to stdout at the end of the run.

## Stage 2 — reflag remediation

`remediate.py` is the **write** stage. It reads the Stage 1 `worklist.csv`,
re-probes each `REFLAG`/`ENCODE` candidate, and — when a compatible audio track
already exists in the file — promotes it to the **default** track with a
metadata-only `mkvpropedit` edit. When the promoted track has no language tag
(or `und`), it also stamps `eng` so Plex labels it correctly. **No audio is ever
re-encoded.**

Because `ENCODE` from Stage 1 includes files that actually *do* have a compatible
track that simply isn't tagged English (e.g. TrueHD default + an untagged AC3
5.1), Stage 2 re-evaluates those too and reflags them when possible. Files that
genuinely have **no compatible audio track at all** can't be reflagged — they are
**deferred** (listed in the summary) for a future Stage 3 re-encode, or left for
the media server to transcode on the fly.

Reflagging is in-place, fast, and reversible (re-run to change the default back).

| Candidate (from Stage 1) | Stage 2 action |
|--------------------------|----------------|
| `REFLAG` | Promote the compatible English track to default; stamp `eng` if untagged. |
| `ENCODE` with an untagged-but-compatible track | Same — promote it (`reflag`). |
| `ENCODE` with no compatible track anywhere | `defer-no-compatible-track` (Stage 3). |
| Non-`.mkv` | `skip-not-mkv` — `mkvpropedit` can't edit MP4 headers. |

### Run

Stage 2 is **dry-run by default** — it prints the planned `mkvpropedit` actions
and changes nothing. Pass `--apply` to actually modify files.

Unlike Stage 1, Stage 2 **writes** to your media, so mount the libraries
**read-write** (omit the `:ro` suffix). The image already ships `mkvtoolnix`;
override the entrypoint to run `remediate.py`:

```sh
# Dry run first — review reports/remediation.csv before applying.
docker run --rm \
  -v /Volumes/Plex/Movies:/media/movies \
  -v /Volumes/Plex/TV:/media/television \
  -v $(pwd)/reports:/reports \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  --entrypoint python plex-audio-audit \
  /app/remediate.py --config /app/config.yaml

# Apply once you're satisfied (note: libraries mounted read-write, no :ro).
docker run --rm \
  -v /Volumes/Plex/Movies:/media/movies \
  -v /Volumes/Plex/TV:/media/television \
  -v $(pwd)/reports:/reports \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  --entrypoint python plex-audio-audit \
  /app/remediate.py --config /app/config.yaml --apply
```

Useful flags: `--worklist PATH` (default `<reports_dir>/worklist.csv`),
`--library NAME` (only remediate one library), `--no-language-fix` (flip the
default flag but don't stamp `eng` on untagged tracks).

### Stage 2 outputs

`reports/remediation.csv` — one row per candidate: `library`, `relative_path`,
`action`, `reason`, `old_default`, `new_default`, `language_stamped`, `verify`,
`error`, and the exact `command` that was (or would be) run. After `--apply`,
each reflagged file is re-probed and `verify` records `ok` or the mismatch.

`reports/remediation_summary.txt` — counts by action plus an explicit **deferred
list** of files that still need attention (no compatible track / not an MKV), so
nothing is silently dropped. Printed to stdout at the end of the run.

## What's next

Stage 3 (re-encode the `defer-no-compatible-track` files to a direct-play codec)
is **not implemented yet**. Those files are reported by Stage 2; for now you can
let the media server transcode them on the fly, or handle them manually.
