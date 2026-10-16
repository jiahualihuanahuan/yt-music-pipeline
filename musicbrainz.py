"""Normalize artist names to MusicBrainz canonical form + enrich metadata.

Uses free MB API (no key). Falls back to iTunes Search and Deezer.
Updates local_db and returns canonical artist name, album, year, artwork URL.
"""
import time
import requests
from config import MB_CONTACT, USER_AGENT
from local_db import (connect, init_db, lookup_artist, upsert_artist,
                      upsert_album, upsert_track, normalize)

MB_API = "https://musicbrainz.org/ws/2"
ITUNES_API = "https://itunes.apple.com/search"
DEEZER_API = "https://api.deezer.com/search"
HEADERS = {"User-Agent": f"{USER_AGENT} ({MB_CONTACT})"}


def _mb(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
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
        "artist": artist_mbid, "fmt": "json", "limit": 50, "inc": "recordings"})
    return data.get("releases", []) if data else []


def enrich(meta):
    """Given parsed meta dict, return enriched meta with canonical artist/album."""
    init_db()
    conn = connect()
    artist_name = meta.get("artist")
    if not artist_name:
        return meta

    # 1. local DB first
    row = lookup_artist(conn, artist_name)
    if row:
        meta["artist_canonical"] = row["name"]
        meta["artist_region"] = row["region"]
        return meta

    # 2. MusicBrainz live lookup
    a = mb_artist(artist_name)
    if a:
        aid = upsert_artist(conn, a["name"], sort_name=a.get("sort-name"),
                            region=None)
        meta["artist_canonical"] = a["name"]
        # grab a recent release for album hint
        rels = mb_releases(a["id"])
        if rels and meta.get("title"):
            for rel in rels:
                titles = [normalize(t.get("title")) for m in rel.get("media", [])
                          for t in m.get("tracks", [])]
                if normalize(meta["title"]) in titles:
                    alid = upsert_album(conn, rel["title"], aid,
                                        year=(rel.get("date") or "")[:4] or None)
                    meta["album"] = rel["title"]
                    meta["album_year"] = (rel.get("date") or "")[:4]
                    break
        conn.commit()
        return meta

    # 3. iTunes fallback
    try:
        r = requests.get(ITUNES_API, params={
            "term": f"{meta.get('title','')} {artist_name}",
            "entity": "song", "limit": 5}, timeout=15)
        for it in r.json().get("results", []):
            if normalize(it.get("artistName", "")) == normalize(artist_name):
                meta["artist_canonical"] = it["artistName"]
                if not meta.get("album"):
                    meta["album"] = it.get("collectionName")
                return meta
    except Exception:
        pass

    # 4. Deezer fallback
    try:
        r = requests.get(DEEZER_API, params={
            "q": f"track:\"{meta.get('title','')}\" artist:\"{artist_name}\""},
            timeout=15)
        for d in r.json().get("data", []):
            if normalize(d.get("artist", {}).get("name", "")) == normalize(artist_name):
                meta["artist_canonical"] = d["artist"]["name"]
                if not meta.get("album"):
                    meta["album"] = d.get("album", {}).get("title")
                return meta
    except Exception:
        pass

    # nothing found: keep original, album stays None -> use title
    meta["artist_canonical"] = artist_name
    return meta
