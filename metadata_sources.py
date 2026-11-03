"""Extra metadata sources used after MusicBrainz: Last.fm (API key) and
Cover Art Archive (MusicBrainz cover art, no key).

lastfm_lookup returns a dict with any of
  artist, title, album, year, genres (list[str]), cover_url
or None when the key is missing, nothing matched, or the call failed.
Results are cached in memory per (title, artist); calls are rate-limited.
"""
import threading
import time
import requests

import config as cfg
from match_guard import accept

MB_API = "https://musicbrainz.org/ws/2"
CAA_API = "https://coverartarchive.org"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"

_lock = threading.Lock()
_last_call = {"lastfm": 0.0, "caa": 0.0, "musicbrainz": 0.0}
_MIN_INTERVAL = {"lastfm": 0.25, "caa": 0.2, "musicbrainz": 1.1}  # Last.fm <=5 req/s, MB 1 req/s
_cache: dict = {}


def _norm(s: str) -> str:
    return "".join(c.lower() for c in (s or "") if c.isalnum())


def _throttle(src: str):
    with _lock:
        wait = _MIN_INTERVAL[src] - (time.time() - _last_call[src])
        if wait > 0:
            time.sleep(wait)
        _last_call[src] = time.time()


def clear_cache():
    _cache.clear()


# ---------------- Cover Art Archive (MusicBrainz) ----------------
def caa_cover_url(release_id: str | None) -> str | None:
    """Front cover URL from the Cover Art Archive for a MusicBrainz release id, or None."""
    if not release_id:
        return None
    key = ("caa", release_id)
    if key in _cache:
        return _cache[key]
    url = f"{CAA_API}/release/{release_id}/front-500"
    out = None
    try:
        _throttle("caa")
        r = requests.head(url, allow_redirects=True, timeout=10,
                          headers={"User-Agent": getattr(cfg, "USER_AGENT", "music-pipeline")})
        out = url if r.status_code < 400 else None
    except Exception:
        out = None
    _cache[key] = out
    return out


def musicbrainz_cover_lookup(title: str, artist: str) -> str | None:
    """Search MusicBrainz recordings for title+artist; return a Cover Art Archive URL."""
    if not title or not artist:
        return None
    key = ("mbcover", _norm(title), _norm(artist))
    if key in _cache:
        return _cache[key]
    out = None
    try:
        _throttle("musicbrainz")
        r = requests.get(f"{MB_API}/recording", params={
            "query": f'recording:"{title}" AND artist:"{artist}"', "fmt": "json", "limit": 5,
        }, headers={"User-Agent": f"{getattr(cfg, 'USER_AGENT', 'music-pipeline')} ({getattr(cfg, 'MB_CONTACT', '')})"},
            timeout=15)
        r.raise_for_status()
        for rec in r.json().get("recordings") or []:
            credit = " ".join(c.get("name", "") for c in rec.get("artist-credit") or [] if isinstance(c, dict))
            if not accept(title, artist, rec.get("title", ""), credit):
                continue
            for rel in rec.get("releases") or []:
                out = caa_cover_url(rel.get("id"))
                if out:
                    break
            if out:
                break
    except Exception as e:
        print(f"musicbrainz cover lookup failed ({artist} - {title}): {e}")
        out = None
    _cache[key] = out
    return out


# ---------------- Last.fm ----------------
_TAG_BLOCK = {"seen live", "favorites", "favourite", "albums i own", "under 2000 listeners"}


def lastfm_lookup(title: str, artist: str) -> dict | None:
    api_key = getattr(cfg, "LASTFM_API_KEY", "")
    if not api_key or not title or not artist:
        return None
    key = ("lastfm", _norm(title), _norm(artist))
    if key in _cache:
        return _cache[key]
    out = None
    try:
        _throttle("lastfm")
        r = requests.get(LASTFM_API, params={
            "method": "track.getInfo", "api_key": api_key, "artist": artist,
            "track": title, "autocorrect": 1, "format": "json",
        }, headers={"User-Agent": getattr(cfg, "USER_AGENT", "music-pipeline")}, timeout=10)
        r.raise_for_status()
        t = r.json().get("track")
        if t and not accept(title, artist, t.get("name", ""), (t.get("artist") or {}).get("name", "")):
            t = None  # autocorrect jumped to a different song
        if t:
            alb = t.get("album") or {}
            imgs = [i.get("#text") for i in alb.get("image") or [] if i.get("#text")]
            tags = [x.get("name", "") for x in ((t.get("toptags") or {}).get("tag") or [])]
            out = {
                "artist": (t.get("artist") or {}).get("name"),
                "title": t.get("name"),
                "album": alb.get("title"),
                "year": None,
                "cover_url": imgs[-1] if imgs else None,
                "genres": [g for g in tags if g and g.lower() not in _TAG_BLOCK][:3],
            }
    except Exception as e:
        print(f"last.fm lookup failed ({artist} - {title}): {e}")
        out = None
    _cache[key] = out
    return out


def supplement(meta: dict) -> dict:
    """Fill missing album/year/genre/cover_url after MusicBrainz.

    Order: MusicBrainz (already applied by musicbrainz.enrich) -> Cover Art
    Archive (cover for the matched MB release) -> Last.fm (album, genre tags,
    cover). Never overwrites values already set.
    """
    title = meta.get("title")
    artist = meta.get("artist_canonical") or meta.get("artist")
    if not title or not artist or meta.get("is_music") is False:
        return meta
    if not meta.get("cover_url") and meta.get("mb_release_id"):
        url = caa_cover_url(meta["mb_release_id"])
        if url:
            meta["cover_url"] = url
    need = [k for k in ("album", "album_year", "genre", "cover_url") if not meta.get(k)]
    if not need:
        return meta
    res = lastfm_lookup(title, artist)
    if not res:
        return meta
    if not meta.get("album") and res.get("album"):
        meta["album"] = res["album"]
        meta.setdefault("album_source", "lastfm")
    if not meta.get("album_year") and res.get("year"):
        meta["album_year"] = res["year"]
    if not meta.get("genre") and res.get("genres"):
        meta["genre"] = res["genres"][0].title()
    if not meta.get("cover_url") and res.get("cover_url"):
        meta["cover_url"] = res["cover_url"]
    return meta
