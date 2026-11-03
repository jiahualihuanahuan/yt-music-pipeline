"""Lyrics from LRCLIB (free, no API key): plain lyrics into the tag, synced
lyrics into a .lrc sidecar.

Lookup order for (artist, title[, album, duration]):
  1. GET /api/get with artist + title + album + duration (exact match)
  2. GET /api/get with artist + title only
  3. GET /api/search (track_name + artist_name), best artist/title match
Instrumental tracks and "not found" are cached and skipped. Results are cached
in the local SQLite DB (table lyrics_cache) so re-runs do not hit the API again.
"""
import threading
import time
from pathlib import Path

import requests

LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
USER_AGENT = "yt-music-pipeline/1.0 (https://github.com/jiahualihuanahuan/yt-music-pipeline)"

_LOCK = threading.Lock()
_LAST_CALL = 0.0
_MIN_INTERVAL = 0.4  # be polite
_mem: dict = {}


def _http(url: str, params: dict):
    global _LAST_CALL
    with _LOCK:
        elapsed = time.time() - _LAST_CALL
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        try:
            r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=12)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception:
            return None
        finally:
            _LAST_CALL = time.time()


def _norm(s: str) -> str:
    return "".join(c.lower() for c in (s or "") if c.isalnum())


# ---------- SQLite cache ----------
def _db():
    try:
        from local_db import connect
        conn = connect()
        conn.execute("""CREATE TABLE IF NOT EXISTS lyrics_cache (
            key TEXT PRIMARY KEY, plain TEXT, synced TEXT, instrumental INTEGER, fetched_at REAL)""")
        return conn
    except Exception:
        return None


def _cache_get(key):
    if key in _mem:
        return _mem[key]
    conn = _db()
    if not conn:
        return None
    try:
        row = conn.execute("SELECT plain, synced, instrumental FROM lyrics_cache WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = {"plain": row[0], "synced": row[1], "instrumental": bool(row[2])}
    _mem[key] = out
    return out


def _cache_put(key, res):
    _mem[key] = res
    conn = _db()
    if not conn:
        return
    try:
        conn.execute("INSERT OR REPLACE INTO lyrics_cache VALUES (?,?,?,?,?)",
                     (key, res["plain"], res["synced"], int(res["instrumental"]), time.time()))
        conn.commit()
    finally:
        conn.close()


def _from_item(item) -> dict:
    return {
        "plain": (item.get("plainLyrics") or "").strip() or None,
        "synced": (item.get("syncedLyrics") or "").strip() or None,
        "instrumental": bool(item.get("instrumental")),
    }


def lookup(title: str, artist: str, album: str | None = None, duration: float | None = None) -> dict | None:
    """Return {"plain", "synced", "instrumental"} or None when nothing was found."""
    title, artist = (title or "").strip(), (artist or "").strip()
    if not title or not artist:
        return None
    key = f"{_norm(artist)}|{_norm(title)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached if (cached["plain"] or cached["synced"] or cached["instrumental"]) else None

    attempts = []
    base = {"artist_name": artist, "track_name": title}
    if album and album.strip() and duration:
        attempts.append({**base, "album_name": album.strip(), "duration": int(round(duration))})
    attempts.append(base)
    res = None
    for params in attempts:
        item = _http(LRCLIB_GET, params)
        if isinstance(item, dict) and (item.get("plainLyrics") or item.get("syncedLyrics") or item.get("instrumental")):
            res = _from_item(item)
            break
    if res is None:
        items = _http(LRCLIB_SEARCH, base) or []
        best = None
        for item in items if isinstance(items, list) else []:
            if not (item.get("plainLyrics") or item.get("syncedLyrics")):
                continue
            if _norm(item.get("artistName")) == _norm(artist) and _norm(item.get("trackName")) == _norm(title):
                best = item
                break
            if best is None and _norm(item.get("trackName")) == _norm(title):
                best = item
        if best:
            res = _from_item(best)
    _cache_put(key, res or {"plain": None, "synced": None, "instrumental": False})
    return res


def fetch_lyrics(title: str, artist: str, album: str | None = None) -> str | None:
    """Plain lyrics or None (kept for existing callers)."""
    res = lookup(title, artist, album)
    return res["plain"] if res and not res["instrumental"] else None


# ---------- tag I/O (mp3 / m4a / flac) ----------
def read_basic(path: Path) -> dict:
    """title, artist, album, duration, has_lyrics for mp3/m4a/flac."""
    out = {"title": "", "artist": "", "album": "", "duration": None, "has_lyrics": False}
    try:
        ext = path.suffix.lower()
        if ext == ".mp3":
            from mutagen.mp3 import MP3
            a = MP3(path)
            t = a.tags or {}
            g = lambda k: str(t[k].text[0]) if k in t and t[k].text else ""
            out.update(title=g("TIT2"), artist=g("TPE1"), album=g("TALB"),
                       has_lyrics=bool(a.tags and a.tags.getall("USLT")))
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            a = MP4(path)
            t = a.tags or {}
            g = lambda k: str(t[k][0]) if t.get(k) else ""
            out.update(title=g("\xa9nam"), artist=g("\xa9ART"), album=g("\xa9alb"), has_lyrics=bool(t.get("\xa9lyr")))
        elif ext == ".flac":
            from mutagen.flac import FLAC
            a = FLAC(path)
            g = lambda k: (a.get(k) or [""])[0]
            out.update(title=g("title"), artist=g("artist"), album=g("album"),
                       has_lyrics=bool(a.get("lyrics") or a.get("unsyncedlyrics")))
        else:
            return out
        out["duration"] = getattr(a.info, "length", None)
    except Exception:
        pass
    return out


def write_plain(path: Path, text: str) -> bool:
    """Write plain lyrics into the file's tag. Never overwrites existing lyrics."""
    ext = path.suffix.lower()
    if ext == ".mp3":
        from mutagen.mp3 import MP3
        from mutagen.id3 import USLT
        a = MP3(path)
        if a.tags is None:
            a.add_tags()
        if a.tags.getall("USLT"):
            return False
        a.tags.add(USLT(encoding=3, lang="und", desc="", text=text))
    elif ext in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4
        a = MP4(path)
        if a.tags is None:
            a.add_tags()
        if a.tags.get("\xa9lyr"):
            return False
        a.tags["\xa9lyr"] = [text]
    elif ext == ".flac":
        from mutagen.flac import FLAC
        a = FLAC(path)
        if a.get("lyrics"):
            return False
        a["lyrics"] = text
    else:
        return False
    a.save()
    return True


def add_lyrics(path: Path) -> str:
    """Fill lyrics for one file. Returns: added / has / instrumental / notfound / notags."""
    info = read_basic(path)
    lrc = path.with_suffix(".lrc")
    if info["has_lyrics"] and lrc.exists():
        return "has"
    if not info["title"] or not info["artist"]:
        return "notags"
    res = lookup(info["title"], info["artist"], info["album"], info["duration"])
    if not res:
        return "notfound"
    if res["instrumental"]:
        return "instrumental"
    did = False
    if res["plain"] and not info["has_lyrics"]:
        did = write_plain(path, res["plain"]) or did
    if res["synced"] and not lrc.exists():
        lrc.write_text(res["synced"] + "\n", encoding="utf-8")
        did = True
    return "added" if did else "has"


AUDIO_EXTS = (".mp3", ".m4a", ".flac")


def lyrics_folder(folder: Path, files=None) -> dict:
    """Fill missing lyrics (+ .lrc) for every mp3/m4a/flac under folder."""
    folder = Path(folder)
    if files is None:
        files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in AUDIO_EXTS and not p.name.startswith("."))
    counts: dict = {}
    for f in files:
        try:
            r = add_lyrics(f)
        except Exception as e:
            print(f"  lyrics failed for {f.name}: {e}")
            r = "error"
        counts[r] = counts.get(r, 0) + 1
        if r == "added":
            print(f"  lyrics: {f.name}")
    print(f"lyrics done: {counts}")
    return counts
