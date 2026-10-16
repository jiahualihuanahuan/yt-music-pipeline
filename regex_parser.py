"""Regex-first metadata parser for YouTube video titles.

Priority: regex patterns -> multi-artist detection (feat/ft/with/by/翻唱/cover)
-> allowlist check -> MusicBrainz check -> flag suspicious for LLM.

Returns dict: title, artist (primary), featured (list), album, confidence, needs_llm.
"""
import re
from local_db import connect, lookup_artist, lookup_track, lookup_album, normalize
from config import ALLOWLIST_PATH
import json

# Patterns ordered by specificity. Each returns (title, artist_raw) or None.
PATTERNS = [
    # "歌名 - 歌手" / "歌名 – 歌手"
    re.compile(r"^(?P<title>.+?)\s*[-–—]\s*(?P<artist>.+?)$"),
    # "歌手 - 歌名" is ambiguous; handled by validation layer, not here
    # "歌名 (Cover by 歌手)" / "歌名【翻唱】歌手"
    re.compile(r"^(?P<title>.+?)\s*[\(\[【].*?(?:[Cc]over\s*[Bb]y|翻唱|cover).*?(?P<artist>.+?)[\)\]】]?$"),
    # "歌手: 歌名"
    re.compile(r"^(?P<artist>.+?)\s*[:：]\s*(?P<title>.+?)$"),
    # "歌名 by 歌手"
    re.compile(r"^(?P<title>.+?)\s+\b[Bb]y\s+(?P<artist>.+?)$"),
]

MULTI_ARTIST_RE = re.compile(
    r"\b(?:feat\.?|ft\.?|featuring|with|\u0026|and|&)\b"
    r"|\u002b|\uFF0B"          # plus signs
    r"|[/,，、]",               # separators
    re.I,
)

NOISE_RE = re.compile(
    r"\b(?:official|audio|video|mv|lyrics?|live|concert|hd|4k|hq|topic)\b",
    re.I,
)

def _split_artists(raw):
    """Split a raw artist string on multi-artist separators. Returns list."""
    if not raw:
        return []
    parts = MULTI_ARTIST_RE.split(raw)
    return [p.strip() for p in parts if p.strip()]


def _load_allowlist():
    if ALLOWLIST_PATH.exists():
        return set(json.loads(ALLOWLIST_PATH.read_text()))
    return set()


def _validate(title, artist, featured):
    """Sanity checks. Returns True if suspicious (should go to LLM)."""
    if NOISE_RE.search(artist or ""):
        return True
    if title and lookup_artist(connect(), title):
        # title itself is a known artist -> likely swapped
        return True
    return False


def parse_title(raw_title):
    """Parse a raw YouTube title. Returns metadata dict."""
    title, artist, featured = None, None, []
    for pat in PATTERNS:
        m = pat.match(raw_title.strip())
        if m:
            title = m.group("title").strip()
            artist_raw = m.group("artist").strip()
            featured = _split_artists(artist_raw)
            artist = featured[0] if featured else artist_raw
            featured = featured[1:]
            break

    suspicious = False
    if title and artist:
        suspicious = _validate(title, artist, featured)
        # cross-check local DB: artist hit = strong, title hit = weak hint only
        db = connect()
        a_hit = lookup_artist(db, artist) is not None
        t_rows = lookup_track(db, title)
        t_hit = len(t_rows) > 0
        if a_hit and not suspicious:
            confidence = "high"
        elif a_hit:
            confidence = "medium"
        elif t_hit:
            confidence = "low"   # title-only hit is weak for covers
        else:
            confidence = "low"
            suspicious = True
    else:
        confidence = "none"
        suspicious = True

    return {
        "title": title,
        "artist": artist,
        "featured": featured,
        "album": None,
        "confidence": confidence,
        "needs_llm": suspicious or confidence in ("none", "low"),
        "raw": raw_title,
    }
