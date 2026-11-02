"""Extra metadata sources: Spotify (client-credentials) and Last.fm (API key).

Both are optional. Each lookup returns a dict with any of
  artist, title, album, year, genres (list[str]), cover_url
or None when the key is missing, nothing matched, or the call failed.
Results are cached in memory per (title, artist); calls are rate-limited.
"""
import base64
import threading
import time
import requests

import config as cfg

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"

_lock = threading.Lock()
_last_call = {"spotify": 0.0, "lastfm": 0.0}
_MIN_INTERVAL = {"spotify": 0.2, "lastfm": 0.25}  # Last.fm asks for <=5 req/s
_cache: dict = {}
_token = {"value": None, "expires": 0.0}


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
    _token.update(value=None, expires=0.0)


# ---------------- Spotify ----------------
def _spotify_token() -> str | None:
    cid = getattr(cfg, "SPOTIFY_CLIENT_ID", "")
    secret = getattr(cfg, "SPOTIFY_CLIENT_SECRET", "")
    if not cid or not secret:
        return None
    if _token["value"] and time.time() < _token["expires"] - 30:
        return _token["value"]
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = requests.post(SPOTIFY_TOKEN_URL, data={"grant_type": "client_credentials"},
                      headers={"Authorization": f"Basic {auth}"}, timeout=10)
    r.raise_for_status()
    j = r.json()
    _token.update(value=j["access_token"], expires=time.time() + int(j.get("expires_in", 3600)))
    return _token["value"]


def _spotify_get(path: str, params=None, token=None):
    _throttle("spotify")
    r = requests.get(f"{SPOTIFY_API}{path}", params=params,
                     headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if r.status_code == 429:
        time.sleep(min(int(r.headers.get("Retry-After", "1")), 10))
        _throttle("spotify")
        r = requests.get(f"{SPOTIFY_API}{path}", params=params,
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def spotify_lookup(title: str, artist: str) -> dict | None:
    if not title or not artist:
        return None
    key = ("spotify", _norm(title), _norm(artist))
    if key in _cache:
        return _cache[key]
    out = None
    try:
        token = _spotify_token()
        if not token:
            return None  # no keys: don't cache, config may change
        data = _spotify_get("/search", {"q": f"track:{title} artist:{artist}", "type": "track", "limit": 5}, token)
        items = (data.get("tracks") or {}).get("items") or []
        want = _norm(artist)
        hit = next((t for t in items if any(_norm(a.get("name")) == want for a in t.get("artists", []))), None)
        if hit:
            alb = hit.get("album") or {}
            imgs = sorted(alb.get("images") or [], key=lambda i: -(i.get("width") or 0))
            out = {
                "artist": hit["artists"][0].get("name"),
                "title": hit.get("name"),
                "album": alb.get("name") if alb.get("album_type") != "compilation" else None,
                "year": (alb.get("release_date") or "")[:4] or None,
                "cover_url": imgs[0]["url"] if imgs else None,
                "genres": [],
            }
            aid = hit["artists"][0].get("id")
            if aid:
                try:
                    out["genres"] = _spotify_get(f"/artists/{aid}", None, token).get("genres") or []
                except Exception:
                    pass
    except Exception as e:
        print(f"spotify lookup failed ({artist} - {title}): {e}")
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
    """Fill missing album/year/genre/cover_url from Spotify, then Last.fm.

    Never overwrites values already set (MusicBrainz / local DB win).
    """
    title = meta.get("title")
    artist = meta.get("artist_canonical") or meta.get("artist")
    if not title or not artist:
        return meta
    for src_name, fn in (("spotify", spotify_lookup), ("lastfm", lastfm_lookup)):
        need = [k for k in ("album", "album_year", "genre", "cover_url") if not meta.get(k)]
        if not need:
            break
        res = fn(title, artist)
        if not res:
            continue
        if not meta.get("album") and res.get("album"):
            meta["album"] = res["album"]
            meta.setdefault("album_source", src_name)
        if not meta.get("album_year") and res.get("year"):
            meta["album_year"] = res["year"]
        if not meta.get("genre") and res.get("genres"):
            meta["genre"] = res["genres"][0].title()
        if not meta.get("cover_url") and res.get("cover_url"):
            meta["cover_url"] = res["cover_url"]
    return meta
