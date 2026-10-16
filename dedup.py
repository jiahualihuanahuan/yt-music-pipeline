"""Deduplicate the music library by similarity (not byte-identity).

Compares normalized "title + artist" strings with rapidfuzz.
Default threshold 85: >= threshold = duplicate, keep higher-quality file.
"""
import shutil
from pathlib import Path
from rapidfuzz import fuzz
from config import PROCESSED_DIR, DEDUP_THRESHOLD

def _key(meta):
    t = (meta.get("title") or "").lower().strip()
    a = (meta.get("artist_canonical") or meta.get("artist") or "").lower().strip()
    return f"{t} {a}"


def dedup(results):
    """results: list of meta dicts with 'file' and processed path info.
    Returns list with duplicates removed (kept = higher quality)."""
    kept = []
    for meta in results:
        k = _key(meta)
        dup = False
        for prev in kept:
            if fuzz.token_sort_ratio(k, _key(prev)) >= DEDUP_THRESHOLD:
                # keep the one with better confidence / longer duration proxy
                if (meta.get("confidence") or "") < (prev.get("confidence") or ""):
                    dup = True
                    break
                else:
                    kept.remove(prev)
                    break
        if not dup:
            kept.append(meta)
    removed = len(results) - len(kept)
    if removed:
        print(f"dedup: removed {removed} duplicates, kept {len(kept)}")
    return kept
