"""Reversible "delete": move unwanted files into DELETE_DIR (never unlink).

Jenny's policy:
  - non-music (LLM is_music=false, confidence high)  -> moved
  - non-music with lower confidence                 -> review list only, not moved
  - 伴奏 / instrumental-karaoke and DJ versions     -> moved
  - livestream song clips (直播歌切, 歌回, live stream) -> moved
Every run writes logs/delete_candidates_<ts>.csv (path, reason, confidence, action)
and every move is logged to logs/deleted_<ts>.csv (src, dest, reason). Moves keep the
path relative to the scanned root and never overwrite (a numeric suffix is added).
"""
from __future__ import annotations

import csv
import re
import shutil
from datetime import datetime
from pathlib import Path

# 伴奏 = backing track. Kept tight: "纯音乐"/"instrumental" alone can be real music.
_INSTRUMENTAL_RE = re.compile(
    r"伴奏|伴唱|karaoke\s*(?:version|ver\.?)|[(（\[]\s*karaoke\s*[)）\]]|off\s*vocal|消音版|去人声|去人聲", re.I)
# "with vocals" markers: a 伴奏 mention that is not a backing track
_HAS_VOCAL_RE = re.compile(r"うた入り|歌入り|带唱|帶唱|原唱版|花絮|彩排", re.I)
# quoted lyrics may contain the word 伴奏
_QUOTED_RE = re.compile(r"『[^』]*』|「[^」]*」")
# DJ remix versions ("DJ版", "DJ0.9降速版", "(DJ抖音版)", "- DJ Remix"), not artists like "DJ Snake".
_DJ_RE = re.compile(
    r"DJ\s*[\w.]{0,12}版|[(（\[【]\s*DJ|\s[-–]\s*DJ(?:\s*(?:remix|mix|版|\d)|\s*$)|DJ\s*(?:remix|抖音|降速|加速|慢摇|慢搖|热播|熱播)",
    re.I,
)


# Livestream song clips (Jenny: not music -> delete). Only clearly marked ones.
_LIVESTREAM_RE = re.compile(
    r"直播歌切|直播切片|直播片段|直播回放|直播唱|直播间|直播間|歌切|(?<!非)歌回|歌枠|"
    r"live\s*stream|livestream|\b(?:twitch|bilibili)\s*(?:live|直播)|[【\[(（]\s*直播\s*[】\])）]",
    re.I,
)


def version_reason(*texts: str) -> str | None:
    """'livestream' / 'instrumental' / 'dj' if any text (filename stem, title tag) marks such a version."""
    for t in texts:
        if not t:
            continue
        t = _QUOTED_RE.sub(" ", t)
        if _LIVESTREAM_RE.search(t):
            return "livestream"
        if _INSTRUMENTAL_RE.search(t) and not _HAS_VOCAL_RE.search(t):
            return "instrumental"
        if _DJ_RE.search(t):
            return "dj"
    return None


class DeleteRun:
    """Collects candidates and performs the moves for one pipeline run."""

    def __init__(self, root: Path, delete_dir: Path, log_dir: Path, enabled: bool = True):
        self.root = Path(root).resolve()
        self.delete_dir = Path(delete_dir)
        self.log_dir = Path(log_dir)
        self.enabled = enabled
        self.ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.candidates: list[dict] = []
        self.moves: list[dict] = []

    def _dest(self, path: Path) -> Path:
        try:
            rel = Path(path).resolve().relative_to(self.root)
        except ValueError:
            rel = Path(Path(path).parent.name) / Path(path).name
        dest = self.delete_dir / rel
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{Path(rel).stem}_{n}{Path(rel).suffix}")
            n += 1
        return dest

    def consider(self, path: Path, reason: str, confidence: str = "high") -> bool:
        """Record a candidate; move it when enabled and confidence is high. True if moved."""
        move = self.enabled and confidence == "high"
        self.candidates.append({"path": str(path), "reason": reason, "confidence": confidence,
                                "action": "moved" if move else ("review" if confidence != "high" else "dry-run")})
        if not move:
            return False
        dest = self._dest(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        for side in (".json", ".lrc"):
            s = Path(path).with_suffix(side)
            if s.exists():
                shutil.move(str(s), str(dest.with_suffix(side)))
        self.moves.append({"src": str(path), "dest": str(dest), "reason": reason})
        print(f"  to-delete ({reason}): {Path(path).name} -> {dest}")
        return True

    def write_logs(self) -> tuple[Path | None, Path | None]:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        cand = self.log_dir / f"delete_candidates_{self.ts}.csv"
        with cand.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["path", "reason", "confidence", "action"])
            w.writeheader()
            w.writerows(self.candidates)
        moved = None
        if self.moves:
            moved = self.log_dir / f"deleted_{self.ts}.csv"
            with moved.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["src", "dest", "reason"])
                w.writeheader()
                w.writerows(self.moves)
        print(f"delete: {len(self.candidates)} candidates, {len(self.moves)} moved to {self.delete_dir} (log {cand})")
        return cand, moved
