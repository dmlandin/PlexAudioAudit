#!/usr/bin/env python3
"""Stage 1 Plex audio audit — read-only library scanner.

Walks configured library roots, runs ffprobe on each video file, extracts every
audio track's metadata, classifies each file, and writes a per-library summary
plus a CSV worklist. Modifies no media files.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

FFPROBE_TIMEOUT_SEC = 60
ENGLISH_TITLE_RE = re.compile(r"english", re.IGNORECASE)

CLASS_OK = "OK"
CLASS_REFLAG = "REFLAG"
CLASS_ENCODE = "ENCODE"
CLASS_SKIP_MP4 = "SKIP-MP4"
CLASS_SKIP = "SKIP"

CLASSIFICATIONS = [CLASS_OK, CLASS_REFLAG, CLASS_ENCODE, CLASS_SKIP_MP4, CLASS_SKIP]


@dataclass
class AudioTrack:
    codec: str
    channels: Optional[int]
    language: str
    title: str
    default: bool


@dataclass
class FileResult:
    library: str
    relative_path: str
    classification: str
    reason: str = ""
    tracks: list[AudioTrack] = field(default_factory=list)


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_ffprobe(path: Path) -> Optional[dict]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-print_format", "json",
                "-show_streams", "-show_format",
                str(path),
            ],
            capture_output=True,
            timeout=FFPROBE_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        sys.exit("error: ffprobe not found on PATH (install ffmpeg)")
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def extract_audio_tracks(probe: dict) -> list[AudioTrack]:
    tracks: list[AudioTrack] = []
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        tracks.append(AudioTrack(
            codec=(stream.get("codec_name") or "").lower(),
            channels=stream.get("channels"),
            language=(tags.get("language") or "und").lower(),
            title=tags.get("title") or "",
            default=bool(disposition.get("default")),
        ))
    return tracks


def classify(tracks: list[AudioTrack], rules: dict) -> tuple[str, str]:
    target_lang = (rules.get("target_language") or "eng").lower()
    ok_codecs = {str(c).lower() for c in (rules.get("ok_codecs") or [])}
    encode_codecs = {str(c).lower() for c in (rules.get("encode_codecs") or [])}

    tagged_eng = [t for t in tracks if t.language == target_lang]
    # Tracks where lang tag is missing/und but title clearly says English —
    # a metadata-only fix is possible at Stage 2.
    title_eng = [
        t for t in tracks
        if t.language in ("", "und") and ENGLISH_TITLE_RE.search(t.title or "")
    ]

    if not tagged_eng and not title_eng:
        if len(tracks) == 1 and tracks[0].codec in ok_codecs:
            return CLASS_OK, "single_compatible_track_unknown_lang"
        return CLASS_ENCODE, "no_english_track"

    if tagged_eng:
        default_eng = [t for t in tagged_eng if t.default]
        if default_eng:
            if any(t.codec in ok_codecs for t in default_eng):
                return CLASS_OK, ""
            codec = default_eng[0].codec or "unknown"
            if any(t.codec in ok_codecs for t in tagged_eng):
                return CLASS_REFLAG, f"default_eng_codec_{codec}_ok_sibling_exists"
            if any(t.codec in encode_codecs for t in default_eng):
                return CLASS_ENCODE, f"default_eng_codec_{codec}"
            return CLASS_ENCODE, f"default_eng_codec_unsupported_{codec}"
        return CLASS_REFLAG, "english_track_not_default"

    # Only title-matched English tracks (lang is und/missing) — metadata fix.
    return CLASS_REFLAG, "english_title_no_lang_tag"


def walk_library(root: Path, extensions: set[str]):
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            if Path(name).suffix.lower() in extensions:
                yield Path(dirpath) / name


def join_tracks(values) -> str:
    return ";".join("" if v is None else str(v) for v in values)


def audit_file(path: Path, library_name: str, root: Path, rules: dict) -> FileResult:
    rel = str(path.relative_to(root))
    ext = path.suffix.lower()

    if ext in (".mp4", ".m4v"):
        probe = run_ffprobe(path)
        tracks = extract_audio_tracks(probe) if probe else []
        return FileResult(library_name, rel, CLASS_SKIP_MP4, reason="container_mp4", tracks=tracks)

    probe = run_ffprobe(path)
    if probe is None:
        return FileResult(library_name, rel, CLASS_SKIP, reason="ffprobe_failed")

    tracks = extract_audio_tracks(probe)
    if not tracks:
        return FileResult(library_name, rel, CLASS_SKIP, reason="no_audio_streams", tracks=tracks)

    cls, reason = classify(tracks, rules)
    return FileResult(library_name, rel, cls, reason=reason, tracks=tracks)


def write_csv(results: list[FileResult], dest: Path) -> None:
    with dest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "library", "relative_path", "classification", "reason",
            "track_count", "codecs", "channels", "languages", "titles", "defaults",
        ])
        for r in results:
            writer.writerow([
                r.library,
                r.relative_path,
                r.classification,
                r.reason,
                len(r.tracks),
                join_tracks(t.codec for t in r.tracks),
                join_tracks(t.channels for t in r.tracks),
                join_tracks(t.language for t in r.tracks),
                join_tracks(t.title for t in r.tracks),
                join_tracks(int(t.default) for t in r.tracks),
            ])


def build_summary(results: list[FileResult], library_names: list[str]) -> str:
    per_lib: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        per_lib[r.library][r.classification] += 1

    lines = ["Plex Audio Audit — Stage 1 Summary", "=" * 40, ""]
    for lib in library_names:
        counts = per_lib.get(lib, Counter())
        total = sum(counts.values())
        lines.append(f"[{lib}]  total files: {total}")
        for cls in CLASSIFICATIONS:
            lines.append(f"  {cls:<10} {counts.get(cls, 0)}")
        lines.append("")

    grand = Counter()
    for c in per_lib.values():
        grand.update(c)
    grand_total = sum(grand.values())
    lines.append(f"[ALL]   total files: {grand_total}")
    for cls in CLASSIFICATIONS:
        lines.append(f"  {cls:<10} {grand.get(cls, 0)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage 1 read-only Plex audio audit",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml (default: ./config.yaml)",
    )
    args = parser.parse_args()

    if not args.config.is_file():
        sys.exit(f"error: config file not found: {args.config}")

    config = load_config(args.config)
    extensions = {str(e).lower() for e in (config.get("scan_extensions") or [])}
    if not extensions:
        sys.exit("error: config has no scan_extensions")
    rules = config.get("classification") or {}
    reports_dir = Path(config.get("reports_dir") or "./reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    library_names: list[str] = []
    results: list[FileResult] = []

    for lib in config.get("library_roots") or []:
        name = lib["name"]
        root = Path(lib["path"])
        library_names.append(name)
        if not root.is_dir():
            print(
                f"warning: library root not found, skipping: {name} ({root})",
                file=sys.stderr,
            )
            continue
        print(f"scanning {name}: {root}", file=sys.stderr)
        scanned = 0
        for video_path in walk_library(root, extensions):
            results.append(audit_file(video_path, name, root, rules))
            scanned += 1
        print(f"  {name}: {scanned} file(s) processed", file=sys.stderr)

    csv_path = reports_dir / "worklist.csv"
    summary_path = reports_dir / "summary.txt"
    write_csv(results, csv_path)
    summary = build_summary(results, library_names)
    summary_path.write_text(summary, encoding="utf-8")

    print()
    print(summary)
    print(f"worklist: {csv_path}")
    print(f"summary:  {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
