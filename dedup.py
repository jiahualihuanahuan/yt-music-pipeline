"""Deduplicate the music library by similarity (not byte-identity).

Compares normalized "title + artist" strings with rapidfuzz.
Default threshold 85: >= threshold = duplicate, keep higher-confidence file.
"""
from rapidfuzz import fuzz
from config import DEDUP_THRESHOLD

CONF_RANK = {"high": 3, "medium": 2, "low": 1, "": 0, None: 0}


def _key(meta):
    t = (meta.get("title") or "").lower().strip()
    a = (meta.get("artist_canonical") or meta.get("artist") or "").lower().strip()
    return f"{t} {a}"


def _rank(meta) -> int:
    return CONF_RANK.get(meta.get("confidence"), 0)


def is_duplicate(meta, kept):
    """Return True if meta is a duplicate of something already in kept.
    If a strictly better version is found, replace it in-place and return False.
    """
    k = _key(meta)
    if not k.strip():
        return False
    for prev in list(kept):
        if fuzz.token_sort_ratio(k, _key(prev)) >= DEDUP_THRESHOLD:
            if _rank(meta) <= _rank(prev):
                return True
            else:
                kept.remove(prev)
                return False
    return False


def dedup(results):
    """results: list of meta dicts with 'file' and processed path info.
    Returns list with duplicates removed (kept = higher quality)."""
    kept = []
    for meta in results:
        if not is_duplicate(meta, kept):
            kept.append(meta)
    removed = len(results) - len(kept)
    if removed:
        print(f"dedup: removed {removed} duplicates, kept {len(kept)}")
    return kept