#!/usr/bin/env python3
"""Stage 2 Plex audio remediation — reflag only.

Reads the Stage 1 worklist, re-probes each REFLAG/ENCODE candidate, and — when a
compatible English audio track already exists in the file — promotes it to the
default track with a metadata-only `mkvpropedit` edit (and stamps an `eng`
language tag when the promoted track is untagged). No audio is ever re-encoded:
files with no compatible audio track at all are deferred to a future Stage 3 (or
left for the media server to transcode on the fly).

Dry-run by default — pass --apply to actually modify files. mkvpropedit edits the
MKV header in place; the change is reversible by re-running.
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from audit import (
    ENGLISH_TITLE_RE,
    AudioTrack,
    extract_audio_tracks,
    load_config,
    run_ffprobe,
)

MKVPROPEDIT_TIMEOUT_SEC = 120

# Classifications produced by Stage 1 that are candidates for a reflag.
CANDIDATE_CLASSES = {"REFLAG", "ENCODE"}

# Per-file remediation actions.
ACT_REFLAG = "reflag"
ACT_ALREADY_OK = "skip-already-ok"
ACT_DEFER = "defer-no-compatible-track"
ACT_NOT_MKV = "skip-not-mkv"
ACT_MISSING = "error-file-missing"
ACT_ERROR = "error"

# Actions whose files still need attention later (Stage 3 / manual).
DEFERRED_ACTIONS = {ACT_DEFER, ACT_NOT_MKV, ACT_MISSING, ACT_ERROR}

ACTION_ORDER = [
    ACT_REFLAG, ACT_ALREADY_OK, ACT_DEFER, ACT_NOT_MKV, ACT_MISSING, ACT_ERROR,
]


@dataclass
class Plan:
    """The decided remediation for one file."""
    action: str
    reason: str = ""
    # 1-based audio-track indices (mkvpropedit `track:aN`).
    old_defaults: list[int] = field(default_factory=list)
    chosen: Optional[int] = None
    stamp_language: bool = False
    # mkvpropedit (selector, name, value) edits, grouped at command-build time.
    edits: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class Outcome:
    library: str
    relative_path: str
    plan: Plan
    command: str = ""
    verify: str = ""
    error: str = ""


def candidate_rank(track: AudioTrack, target_lang: str) -> Optional[int]:
    """Lower is better. None means the track is not a reflag candidate.

    Only compatible-codec tracks that are plausibly English qualify: explicitly
    tagged target language, an English title on an untagged track, or simply an
    untagged track. A track tagged a *different* language is never a candidate.
    """
    if track.language == target_lang:
        return 0
    if track.language in ("", "und"):
        if ENGLISH_TITLE_RE.search(track.title or ""):
            return 1
        return 2
    return None  # tagged a specific non-target language


def decide(tracks: list[AudioTrack], rules: dict, fix_language: bool) -> Plan:
    """Choose which (if any) audio track to promote to default."""
    target_lang = (rules.get("target_language") or "eng").lower()
    ok_codecs = {str(c).lower() for c in (rules.get("ok_codecs") or [])}

    old_defaults = [i + 1 for i, t in enumerate(tracks) if t.default]

    candidates = []
    for idx, t in enumerate(tracks):
        if t.codec not in ok_codecs:
            continue
        rank = candidate_rank(t, target_lang)
        if rank is None:
            continue
        candidates.append((rank, -(t.channels or 0), idx, t))

    if not candidates:
        return Plan(ACT_DEFER, reason="no_compatible_english_track",
                    old_defaults=old_defaults)

    # If an audio track that's already default is itself a valid candidate, keep
    # it — don't churn the default just to gain channels. Only when no current
    # default qualifies do we pick the best candidate (rank, then most channels).
    candidate_idxs = {idx for _, _, idx, _ in candidates}
    default_candidates = [i for i, t in enumerate(tracks) if t.default and i in candidate_idxs]
    if default_candidates:
        chosen_idx = default_candidates[0]
    else:
        candidates.sort(key=lambda c: (c[0], c[1], c[2]))
        chosen_idx = candidates[0][2]
    chosen = tracks[chosen_idx]
    stamp = fix_language and chosen.language in ("", "und")

    # Minimal edits: only flip flags/tags that actually need to change.
    edits: list[tuple[str, str, str]] = []
    for idx, t in enumerate(tracks):
        sel = f"track:a{idx + 1}"
        if idx == chosen_idx:
            if not t.default:
                edits.append((sel, "flag-default", "1"))
            if stamp:
                edits.append((sel, "language", target_lang))
        elif t.default:
            edits.append((sel, "flag-default", "0"))

    if not edits:
        return Plan(ACT_ALREADY_OK, reason="default_already_correct",
                    old_defaults=old_defaults, chosen=chosen_idx + 1)

    return Plan(
        ACT_REFLAG,
        reason=f"promote_track_a{chosen_idx + 1}_{chosen.codec}",
        old_defaults=old_defaults,
        chosen=chosen_idx + 1,
        stamp_language=stamp,
        edits=edits,
    )


def build_command(path: Path, edits: list[tuple[str, str, str]]) -> list[str]:
    """Group per-track edits into a single mkvpropedit invocation."""
    grouped: "OrderedDict[str, list[tuple[str, str]]]" = OrderedDict()
    for sel, name, value in edits:
        grouped.setdefault(sel, []).append((name, value))
    cmd = ["mkvpropedit", str(path)]
    for sel, sets in grouped.items():
        cmd += ["--edit", sel]
        for name, value in sets:
            cmd += ["--set", f"{name}={value}"]
    return cmd


def run_mkvpropedit(cmd: list[str]) -> tuple[bool, str]:
    """Run mkvpropedit. Returns (ok, message). rc 0/1 (warnings) count as ok."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=MKVPROPEDIT_TIMEOUT_SEC, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "mkvpropedit timed out"
    except FileNotFoundError:
        sys.exit("error: mkvpropedit not found on PATH (install mkvtoolnix)")
    out = (result.stdout or b"").decode("utf-8", "replace").strip()
    err = (result.stderr or b"").decode("utf-8", "replace").strip()
    msg = "; ".join(m for m in (out, err) if m)
    # mkvpropedit: 0 = success, 1 = warnings (changes applied), 2 = error.
    return result.returncode in (0, 1), msg


def verify(path: Path, plan: Plan, target_lang: str) -> str:
    """Re-probe and confirm the intended track is now the sole default."""
    probe = run_ffprobe(path)
    if probe is None:
        return "FAILED: re-probe failed"
    tracks = extract_audio_tracks(probe)
    defaults = [i + 1 for i, t in enumerate(tracks) if t.default]
    if defaults != [plan.chosen]:
        return f"FAILED: default tracks are {defaults}, expected [{plan.chosen}]"
    if plan.stamp_language and plan.chosen is not None:
        lang = tracks[plan.chosen - 1].language
        if lang != target_lang:
            return f"FAILED: language is {lang!r}, expected {target_lang!r}"
    return "ok"


def fmt_defaults(indices: list[int]) -> str:
    return ";".join(str(i) for i in indices) if indices else "none"


def read_worklist(path: Path, library_filter: Optional[str]) -> list[tuple[str, str, str]]:
    """Return (library, relative_path, classification) for candidate rows."""
    rows: list[tuple[str, str, str]] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            classification = (row.get("classification") or "").strip()
            if classification not in CANDIDATE_CLASSES:
                continue
            library = row.get("library") or ""
            if library_filter and library != library_filter:
                continue
            rows.append((library, row.get("relative_path") or "", classification))
    return rows


def remediate_file(
    abs_path: Path, library: str, rel: str, rules: dict, fix_language: bool, apply: bool,
) -> Outcome:
    if abs_path.suffix.lower() != ".mkv":
        return Outcome(library, rel, Plan(ACT_NOT_MKV, reason="not_an_mkv_container"))
    if not abs_path.is_file():
        return Outcome(library, rel, Plan(ACT_MISSING, reason="file_not_found"))

    probe = run_ffprobe(abs_path)
    if probe is None:
        return Outcome(library, rel, Plan(ACT_ERROR, reason="ffprobe_failed"))
    tracks = extract_audio_tracks(probe)
    if not tracks:
        return Outcome(library, rel, Plan(ACT_ERROR, reason="no_audio_streams"))

    plan = decide(tracks, rules, fix_language)
    outcome = Outcome(library, rel, plan)
    if plan.action != ACT_REFLAG:
        return outcome

    cmd = build_command(abs_path, plan.edits)
    outcome.command = " ".join(cmd)
    if not apply:
        outcome.verify = "(dry-run)"
        return outcome

    ok, msg = run_mkvpropedit(cmd)
    if not ok:
        outcome.plan = Plan(ACT_ERROR, reason="mkvpropedit_failed", chosen=plan.chosen)
        outcome.error = msg
        return outcome
    target_lang = (rules.get("target_language") or "eng").lower()
    outcome.verify = verify(abs_path, plan, target_lang)
    if msg:
        outcome.error = msg
    return outcome


def write_report(outcomes: list[Outcome], dest: Path) -> None:
    with dest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "library", "relative_path", "action", "reason",
            "old_default", "new_default", "language_stamped", "verify",
            "error", "command",
        ])
        for o in outcomes:
            p = o.plan
            writer.writerow([
                o.library, o.relative_path, p.action, p.reason,
                fmt_defaults(p.old_defaults),
                p.chosen if p.chosen is not None else "",
                "yes" if p.stamp_language else "",
                o.verify, o.error, o.command,
            ])


def build_summary(outcomes: list[Outcome], apply: bool) -> str:
    counts = Counter(o.plan.action for o in outcomes)
    mode = "APPLY" if apply else "DRY-RUN (no files modified)"
    lines = [
        "Plex Audio Audit — Stage 2 Reflag Summary",
        "=" * 42,
        f"mode: {mode}",
        f"candidates: {len(outcomes)}",
        "",
    ]
    for act in ACTION_ORDER:
        lines.append(f"  {act:<28} {counts.get(act, 0)}")

    failed = [o for o in outcomes if o.verify.startswith("FAILED")]
    if failed:
        lines += ["", f"VERIFY FAILURES ({len(failed)}):"]
        lines += [f"  [{o.library}] {o.relative_path} — {o.verify}" for o in failed]

    deferred = [o for o in outcomes if o.plan.action in DEFERRED_ACTIONS]
    if deferred:
        lines += ["", f"Deferred — need Stage 3 / on-the-fly transcode ({len(deferred)}):"]
        lines += [
            f"  [{o.library}] {o.relative_path} ({o.plan.action}: {o.plan.reason})"
            for o in deferred
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 2 reflag remediation")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"),
                        help="Path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--worklist", type=Path, default=None,
                        help="Path to Stage 1 worklist.csv (default: <reports_dir>/worklist.csv)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually modify files (default: dry-run)")
    parser.add_argument("--no-language-fix", action="store_true",
                        help="Do not stamp an 'eng' language tag on promoted untagged tracks")
    parser.add_argument("--library", default=None,
                        help="Only remediate files from this library name")
    args = parser.parse_args()

    if not args.config.is_file():
        sys.exit(f"error: config file not found: {args.config}")
    config = load_config(args.config)
    rules = config.get("classification") or {}
    reports_dir = Path(config.get("reports_dir") or "./reports")

    worklist = args.worklist or (reports_dir / "worklist.csv")
    if not worklist.is_file():
        sys.exit(f"error: worklist not found: {worklist} (run Stage 1 audit.py first)")

    lib_roots = {lib["name"]: Path(lib["path"]) for lib in (config.get("library_roots") or [])}
    fix_language = not args.no_language_fix

    candidates = read_worklist(worklist, args.library)
    print(f"{'applying' if args.apply else 'dry-run'}: {len(candidates)} candidate file(s)",
          file=sys.stderr)

    outcomes: list[Outcome] = []
    for library, rel, _classification in candidates:
        root = lib_roots.get(library)
        if root is None:
            outcomes.append(Outcome(library, rel,
                                    Plan(ACT_ERROR, reason="unknown_library_in_worklist")))
            continue
        outcomes.append(
            remediate_file(root / rel, library, rel, rules, fix_language, args.apply)
        )

    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "remediation.csv"
    summary_path = reports_dir / "remediation_summary.txt"
    write_report(outcomes, report_path)
    summary = build_summary(outcomes, args.apply)
    summary_path.write_text(summary, encoding="utf-8")

    print()
    print(summary)
    print(f"report:  {report_path}")
    print(f"summary: {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
