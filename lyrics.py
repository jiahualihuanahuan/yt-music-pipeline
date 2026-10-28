"""Fetch plain lyrics from LRCLIB (no API key) and embed as ID3 USLT.

Search uses primary artist + title only. Featured artists are ignored.
Missing lyrics is normal for obscure tracks — callers should treat None as "skip".
"""
import threading
import time
import requests

LRCLIB_SEARCH = "https://lrclib.net/api/search"
USER_AGENT = "yt-music-pipeline/1.0 (https://github.com/jiahualihuanahuan/yt-music-pipeline)"

_LOCK = threading.Lock()
_LAST_CALL = 0.0
_MIN_INTERVAL = 0.4  # be polite


def _get(params: dict) -> list:
    global _LAST_CALL
    with _LOCK:
        elapsed = time.time() - _LAST_CALL
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        try:
            r = requests.get(
                LRCLIB_SEARCH,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=12,
            )
            _LAST_CALL = time.time()
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            _LAST_CALL = time.time()
            return []


def fetch_lyrics(title: str, artist: str, album: str | None = None) -> str | None:
    """Return plain lyrics for primary artist + title, or None.

    Prefer exact-ish title+artist match. Album is optional extra signal.
    """
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title or not artist:
        return None

    # Prefer fielded search first
    params = {"track_name": title, "artist_name": artist}
    if album and album.strip() and album.strip().lower() != title.lower():
        params["album_name"] = album.strip()
    results = _get(params)

    # Fallback: free-text query
    if not results:
        results = _get({"q": f"{title} {artist}"})

    if not results:
        return None

    # Prefer a result whose artist matches and has plain lyrics
    def _norm(s: str) -> str:
        return "".join(c.lower() for c in s if c.isalnum() or c.isspace()).strip()

    want_a = _norm(artist)
    want_t = _norm(title)

    best = None
    for item in results:
        plain = (item.get("plainLyrics") or "").strip()
        if not plain:
            continue
        got_a = _norm(item.get("artistName") or "")
        got_t = _norm(item.get("trackName") or "")
        if got_a == want_a and got_t == want_t:
            return plain
        if best is None:
            best = plain
    return best
