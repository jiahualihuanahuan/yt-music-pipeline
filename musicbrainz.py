"""Normalize artist names to MusicBrainz canonical form + enrich metadata.

Uses local SQLite cache first (both track+album and artist), then live MB API
(rate-limited), falling back to iTunes Search and Deezer.
"""
import threading
import time
import requests
from config import MB_CONTACT, USER_AGENT, ARTIST_QUERIES
from local_db import (
    connect, init_db, lookup_artist, lookup_track,
    upsert_artist, upsert_album, upsert_track, normalize,
)

MB_API = "[https://musicbrainz.org/ws/2](https://musicbrainz.org/ws/2)"
ITUNES_API = "[https://itunes.apple.com/search](https://itunes.apple.com/search)"
DEEZER_API = "[https://api.deezer.com/search](https://api.deezer.com/search)"
HEADERS = {"User-Agent": f"{USER_AGENT} ({MB_CONTACT})"}

_SEED_CASING = {normalize(name): name for name, _, _ in ARTIST_QUERIES}
_MB_LOCK = threading.Lock()
_LAST_MB_CALL = 0.0


def _mb(url, params=None):
    global _LAST_MB_CALL
    with _MB_LOCK:
        elapsed = time.time() - _LAST_MB_CALL
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        _LAST_MB_CALL = time.time()
    if r.status_code == 503:
        time.sleep(3)
        return _mb(url, params)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def mb_artist(name):
    data = _mb(f"{MB_API}/artist", params={"query": name, "fmt": "json", "limit": 5})
    if not data:
        return None
    target = normalize(name)
    for a in data.get("artists", []):
        if normalize(a.get("name", "")) == target:
            return a
    return data["artists"][0] if data.get("artists") else None


def mb_releases(artist_mbid):
    data = _mb(f"{MB_API}/release", params={
        "artist": artist_mbid, "fmt": "json", "limit": 50, "inc": "recordings",
    })
    return data.get("releases", []) if data else []


def _proper_case(name: str, db_display: str | None = None) -> str:
    """Ensure we never return an all-lowercase normalized string when a seed or display name exists."""
    n = normalize(name)
    if n in _SEED_CASING:
        return _SEED_CASING[n]
    if db_display and db_display != n:
        return db_display
    return name


def enrich(meta):
    """Given parsed meta dict, return enriched meta with canonical artist, album, and year."""
    init_db()
    conn = connect()
    try:
        artist_name = meta.get("artist")
        title_name = meta.get("title")
        if not artist_name:
            return meta

        # 1. Local DB Track + Artist Lookup (populates canonical artist, album, and year instantly)
        if title_name:
            track_rows = lookup_track(conn, title_name, artist_name)
            if track_rows:
                best = track_rows[0]
                meta["artist_canonical"] = _proper_case(artist_name, best["artist"])
                meta["artist_region"] = best["region"]
                if best["album"] and not meta.get("album"):
                    meta["album"] = best["album"]
                if best["year"] and not meta.get("album_year"):
                    meta["album_year"] = str(best["year"])
                meta["confidence"] = "high"
                return meta

        # 2. Local DB Artist Lookup
        row = lookup_artist(conn, artist_name)
        if row:
            disp = row["display_name"] if "display_name" in row.keys() else None
            meta["artist_canonical"] = _proper_case(artist_name, disp)
            meta["artist_region"] = row["region"]
            # Do NOT return early if album is still missing—try iTunes/Deezer below for the album!

        # 3. Live MusicBrainz Lookup (only if artist wasn't in local DB)
        if not row:
            try:
                a = mb_artist(artist_name)
            except Exception:
                a = None
            if a:
                aid = upsert_artist(conn, a["name"], sort_name=a.get("sort-name"), region=None)
                meta["artist_canonical"] = _proper_case(a["name"], a["name"])
                rels = mb_releases(a["id"])
                if rels and title_name:
                    norm_t = normalize(title_name)
                    for rel in rels:
                        rel_year = (rel.get("date") or "")[:4] or None
                        alid = upsert_album(conn, rel.get("title", ""), aid, year=rel_year)
                        for m in rel.get("media", []):
                            for t in m.get("tracks", []):
                                upsert_track(conn, t.get("title", ""), alid, aid, duration_ms=t.get("length"))
                                if normalize(t.get("title", "")) == norm_t and not meta.get("album"):
                                    meta["album"] = rel.get("title")
                                    meta["album_year"] = rel_year
                conn.commit()
                if meta.get("album"):
                    return meta

        # 4. iTunes Search Fallback (for canonical artist or missing album)
        if not meta.get("album") or not meta.get("artist_canonical"):
            try:
                r = requests.get(
                    ITUNES_API,
                    params={"term": f"{title_name or ''} {artist_name}", "entity": "song", "limit": 5},
                    timeout=12,
                )
                for it in r.json().get("results", []):
                    if normalize(it.get("artistName", "")) == normalize(artist_name):
                        meta["artist_canonical"] = meta.get("artist_canonical") or it["artistName"]
                        if not meta.get("album") and it.get("collectionName"):
                            meta["album"] = it["collectionName"]
                        if not meta.get("album_year") and it.get("releaseDate"):
                            meta["album_year"] = it["releaseDate"][:4]
                        return meta
            except Exception:
                pass

        # 5. Deezer Fallback
        if not meta.get("album") or not meta.get("artist_canonical"):
            try:
                r = requests.get(
                    DEEZER_API,
                    params={"q": f'track:"{title_name or ""}" artist:"{artist_name}"'},
                    timeout=12,
                )
                for d in r.json().get("data", []):
                    if normalize(d.get("artist", {}).get("name", "")) == normalize(artist_name):
                        meta["artist_canonical"] = meta.get("artist_canonical") or d["artist"]["name"]
                        if not meta.get("album") and d.get("album", {}).get("title"):
                            meta["album"] = d["album"]["title"]
                        return meta
            except Exception:
                pass

        if not meta.get("artist_canonical"):
            meta["artist_canonical"] = _proper_case(artist_name)
        return meta
    finally:
        conn.close()