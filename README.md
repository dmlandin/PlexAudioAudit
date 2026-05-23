# plex-audio-audit

Stage 1 of a Plex library audio audit tool. **Read-only** — walks your media
libraries, inspects each video file's audio tracks via `ffprobe`, classifies
what (if any) action each file will need at Stage 2, and writes a per-library
summary plus a CSV worklist. No media files are modified.

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

## What's next

Stage 2 (remux to fix `REFLAG` files and re-encode `ENCODE` files) is **not
implemented yet** — review the Stage 1 output against your library first.
