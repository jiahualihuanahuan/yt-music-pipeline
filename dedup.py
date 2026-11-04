"""Near-duplicate detection for the music library (not byte-identity).

Two files are the "same song" only when ALL of these hold (accuracy first):
  - both have a non-empty title and artist tag,
  - normalized artist and normalized title are equal
    (case/width/punctuation/whitespace-insensitive),
  - both durations are known and differ by <= DEDUP_DURATION_TOLERANCE seconds.
Anything missing or ambiguous is NOT a duplicate.

Better copy = higher bitrate, then more/longer tags, then larger file,
then the name without a `_<digits>` collision suffix.
Losers are never deleted: callers hand them to deletion.DeleteRun (quarantine + CSV).
"""
from __future__ import annotations

import csv
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

try:
    from config import DEDUP_DURATION_TOLERANCE
except Exception:  # pragma: no cover
    DEDUP_DURATION_TOLERANCE = 2.5

SUFFIX_RE = re.compile(r"^(?P<base>.+)_\d+$")
_PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return _PUNCT_RE.sub("", s)


def audio_info(path: Path) -> dict:
    """title/artist/duration/bitrate/tag score/size for one MP3 (empty values on failure)."""
    path = Path(path)
    info = {"path": path, "title": "", "artist": "", "duration": 0.0, "bitrate": 0,
            "tag_frames": 0, "tag_chars": 0, "size": 0}
    try:
        info["size"] = path.stat().st_size
    except OSError:
        pass
    try:
        from mutagen.mp3 import MP3
        a = MP3(path)
        info["duration"] = float(getattr(a.info, "length", 0) or 0)
        info["bitrate"] = int(getattr(a.info, "bitrate", 0) or 0)
        tags = a.tags or {}
        for key, frame in tags.items():
            text = getattr(frame, "text", None)
            if text is not None:
                joined = " ".join(str(t) for t in text).strip()
                if not joined:
                    continue
                info["tag_chars"] += len(joined)
            info["tag_frames"] += 1
        def _get(k):
            v = tags.get(k)
            return str(v.text[0]).strip() if v is not None and getattr(v, "text", None) else ""
        info["title"] = _get("TIT2")
        info["artist"] = _get("TPE1")
    except Exception:
        pass
    return info


def song_key(info: dict) -> tuple[str, str] | None:
    a, t = norm(info.get("artist")), norm(info.get("title"))
    if not a or not t:
        return None
    return (a, t)


def is_near_duplicate(a: dict, b: dict, tol: float | None = None) -> bool:
    tol = DEDUP_DURATION_TOLERANCE if tol is None else tol
    ka, kb = song_key(a), song_key(b)
    if ka is None or ka != kb:
        return False
    da, db = a.get("duration") or 0, b.get("duration") or 0
    if da <= 0 or db <= 0:
        return False
    return abs(da - db) <= tol


def quality(info: dict) -> tuple:
    unsuffixed = 0 if SUFFIX_RE.match(Path(info["path"]).stem) else 1
    return (info.get("bitrate", 0), info.get("tag_frames", 0), info.get("tag_chars", 0),
            info.get("size", 0), unsuffixed)


def better(a: dict, b: dict) -> dict:
    """Return the better of two copies (a wins ties)."""
    return a if quality(a) >= quality(b) else b


def group_duplicates(infos: list[dict], tol: float | None = None) -> list[list[dict]]:
    """Groups (size >= 2) of near-duplicates. Within a song key, files cluster when
    within tol seconds of the cluster's first (shortest) member."""
    tol = DEDUP_DURATION_TOLERANCE if tol is None else tol
    by_key: dict = {}
    for i in infos:
        k = song_key(i)
        if k is not None and (i.get("duration") or 0) > 0:
            by_key.setdefault(k, []).append(i)
    groups = []
    for items in by_key.values():
        items.sort(key=lambda x: x["duration"])
        cluster = [items[0]]
        for it in items[1:]:
            if it["duration"] - cluster[0]["duration"] <= tol:
                cluster.append(it)
            else:
                if len(cluster) > 1:
                    groups.append(cluster)
                cluster = [it]
        if len(cluster) > 1:
            groups.append(cluster)
    return groups


def scan_folder(folder: Path, delete_run, log_dir: Path, apply: bool = False,
                tol: float | None = None) -> tuple[list[list[dict]], Path]:
    """Find near-duplicate groups under folder; send losers to delete_run (quarantine).

    apply=False: dry run (delete_run should be created with enabled=False).
    After moving the losers, a `_<digits>` winner is renamed back to the canonical
    name only when that name belonged to a quarantined loser.
    Writes log_dir/dedup_<ts>.csv. Returns (groups, report_path).
    """
    folder = Path(folder)
    files = [p for p in sorted(folder.rglob("*.mp3")) if not p.name.startswith(".")]
    print(f"dedup: scanning {len(files)} MP3s under {folder} ({'APPLY' if apply else 'dry run'}) ...")
    infos = [audio_info(p) for p in files]
    groups = group_duplicates(infos, tol)
    rows = []
    for g in groups:
        keep = g[0]
        for it in g[1:]:
            keep = better(keep, it)
        losers = [x for x in g if x is not keep]
        for lo in losers:
            moved = delete_run.consider(lo["path"], f"near-duplicate of {keep['path']}", "high")
            rows.append({"group_artist": keep["artist"], "group_title": keep["title"],
                         "keep": str(keep["path"]), "keep_bitrate": keep["bitrate"],
                         "keep_duration": round(keep["duration"], 2),
                         "loser": str(lo["path"]), "loser_bitrate": lo["bitrate"],
                         "loser_duration": round(lo["duration"], 2),
                         "action": "moved" if moved else "dry-run"})
        if apply:
            m = SUFFIX_RE.match(keep["path"].stem)
            if m:
                target = keep["path"].with_name(m.group("base") + keep["path"].suffix)
                if (not target.exists() and any(Path(lo["path"]) == target for lo in losers)):
                    shutil.move(str(keep["path"]), str(target))
                    lrc = keep["path"].with_suffix(".lrc")
                    if lrc.exists() and not target.with_suffix(".lrc").exists():
                        shutil.move(str(lrc), str(target.with_suffix(".lrc")))
                    print(f"  renamed winner -> {target.name}")
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    report = log_dir / f"dedup_{datetime.now():%Y%m%d_%H%M%S}.csv"
    fields = ["group_artist", "group_title", "keep", "keep_bitrate", "keep_duration",
              "loser", "loser_bitrate", "loser_duration", "action"]
    with report.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"dedup: {len(groups)} groups, {len(rows)} losers ({'moved' if apply else 'not moved, dry run'}); report {report}")
    return groups, report
