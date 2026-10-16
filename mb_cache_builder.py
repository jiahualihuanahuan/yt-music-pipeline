"""Populate local_db from MusicBrainz for every artist in ARTIST_QUERIES.

Run once or weekly before the main pipeline. ~1 req/sec per MB policy.
"""
import time
import requests
from config import ARTIST_QUERIES, MB_CONTACT, USER_AGENT
from local_db import (connect, init_db, upsert_artist, upsert_album,
                      upsert_track, stats, normalize)

MB_API = "https://musicbrainz.org/ws/2"
HEADERS = {"User-Agent": f"{USER_AGENT} ({MB_CONTACT})"}
SLEEP = 1.1


def mb_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if r.status_code == 503:
        time.sleep(5)
        return mb_get(url, params)
    r.raise_for_status()
    return r.json()


def search_artist(name):
    data = mb_get(f"{MB_API}/artist", params={"query": name, "fmt": "json", "limit": 5})
    return data.get("artists", [])


def get_releases(artist_mbid, limit=100):
    data = mb_get(f"{MB_API}/release", params={
        "artist": artist_mbid, "fmt": "json", "limit": limit, "inc": "recordings"})
    return data.get("releases", [])


def cache_artist(conn, name, region):
    rows = search_artist(name)
    if not rows:
        return False
    target = normalize(name)
    best = next((a for a in rows if normalize(a.get("name", "")) == target), rows[0])
    aid = upsert_artist(conn, best.get("name", name),
                        sort_name=best.get("sort-name"), region=region)
    try:
        releases = get_releases(best["id"])
    except Exception:
        return False
    for rel in releases:
        alid = upsert_album(conn, rel.get("title", ""), aid,
                            year=(rel.get("date") or "")[:4] or None)
        for medium in rel.get("media", []):
            for rec in medium.get("tracks", []):
                upsert_track(conn, rec.get("title", ""), alid, aid,
                             duration_ms=rec.get("length"))
    conn.commit()
    return True


def main():
    init_db()
    conn = connect()
    ok = skip = 0
    seen = set()
    for name, region, weight in ARTIST_QUERIES:
        key = normalize(name)
        if key in seen:
            continue
        seen.add(key)
        print(f"caching {name} ({region})...", end=" ", flush=True)
        try:
            if cache_artist(conn, name, region):
                print("ok"); ok += 1
            else:
                print("no match"); skip += 1
        except Exception as e:
            print("ERR", e); skip += 1
        time.sleep(SLEEP)
    conn.close()
    print("DONE cached=", ok, "skipped=", skip, "stats=", stats())


if __name__ == "__main__":
    main()
