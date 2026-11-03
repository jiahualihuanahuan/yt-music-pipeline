"""Similarity guard for enrichment results.

A lookup result (MusicBrainz / Last.fm / iTunes / Deezer) is only
accepted when BOTH its artist and title are close to what we searched for.
Comparison ignores case, punctuation, bracketed suffixes ("(Remastered)",
"(From Moana)"), fullwidth forms and Traditional vs Simplified Chinese.
"""
import re
import unicodedata

from rapidfuzz import fuzz

try:  # optional: Traditional <-> Simplified folding
    import opencc
    _t2s = opencc.OpenCC("t2s").convert
except Exception:  # pragma: no cover - optional dependency
    _t2s = lambda s: s

TITLE_MIN = 85
ARTIST_MIN = 85
_BRACKETS = re.compile(r"[(\[（【《「『][^)\]）】》」』]*[)\]）】》」』]")
_SUFFIX = re.compile(r"\s+-\s+(remaster(ed)?|live|radio edit|single version|from .*)$", re.I)


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = _t2s(s).lower()
    s = _SUFFIX.sub("", _BRACKETS.sub(" ", s))
    return "".join(c for c in s if c.isalnum() or c == " ").strip()


def fold(s: str | None) -> str:
    """NFKC + Traditional->Simplified + lowercase, letters/digits only (no bracket stripping)."""
    s = _t2s(unicodedata.normalize("NFKC", s or "")).lower()
    return "".join(c for c in s if c.isalnum())


def _score(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a.replace(" ", "") == b.replace(" ", ""):
        return 100.0
    return fuzz.ratio(a, b)


def title_ok(want: str, got: str) -> bool:
    return _score(want, got) >= TITLE_MIN


def artist_ok(want: str, got: str) -> bool:
    """Artist may be a list ("A, B" / "A & B") on either side: any pair matching is enough."""
    split = lambda s: [p for p in re.split(r"\s*(?:,|&|/|、| feat\.? | ft\.? | x )\s*", s or "", flags=re.I) if p]
    return any(_score(w, g) >= ARTIST_MIN for w in split(want) or [want] for g in split(got) or [got])


def accept(want_title: str, want_artist: str, got_title: str, got_artist: str) -> bool:
    return title_ok(want_title, got_title) and artist_ok(want_artist, got_artist)
