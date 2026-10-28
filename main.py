#!/usr/bin/env python3
"""Music pipeline orchestrator.

Usage:
  python main.py                              # full run (download + post-process in parallel)
  python main.py --dry-run                    # search + parse only, no downloads
  python main.py --skip-dl                    # skip download phase, process existing staging
  python main.py --reprocess /path/to/music   # retag all MP3s in folder (in place)
  python main.py --fill-missing /path/to/music # only retag files missing title/artist/album
  python main.py --llm-only /path/to/music    # force LLM inference only (overwrite tags, no regex)
  python main.py --remove-tags /path/to/music # strip text ID3 tags (keep cover art)
  python main.py --lyrics /path/to/music       # add lyrics (USLT) for files with title+artist
"""
import argparse
import json
import shutil
import threading
import time
import re
from pathlib import Path
from datetime import datetime
import glob
from config import (
    STAGING_DIR, PROCESSED_DIR, LOG_DIR, DRY_RUN,
    LLM_BATCH_SIZE,
)
from search_download import main as run_downloads
from parse_meta import parse_one, apply_llm_batch, finalize_allowlist, parse_all
from musicbrainz import enrich
from lyrics import fetch_lyrics
from dedup import dedup, is_duplicate
from artwork import process_cover
from local_db import init_db, stats
from search_download import main as run_downloads, download_url

CHANNEL_NOISE = {
    "official", "channel", "topic", "vevo", "music", "records", "entertainment",
    "official audio", "official video", "official mv", "lyrics", "hd", "4k",
}

# Albums that should never be used; fall back to title
INVALID_ALBUMS = {
    "", "unknown", "unknown album", "no album", "未知专辑", "none", "null",
    "music", "songs", "singles", "音乐", "歌曲", "专辑",
    "🎵", "🎶", "🎧", "🎤", "🎸", "🎹", "🎼", "🎻", "🔊", "🎧", "🎵🎶",
    "n/a", "na", "tbd", "various", "various artists", "va",
}

# Artists that should never be used
INVALID_ARTISTS = {
    "", "unknown", "unknown artist", "various artists", "va", "群星", "未知歌手", "none", "null",
}

# Regex: album is only punctuation, symbols, or emoji
_JUNK_ALBUM_RE = re.compile(r"^[\s\W\u2600-\u27BF\U0001F300-\U0001F9FF]+$", re.UNICODE)


def _enrich_album_first(meta: dict) -> dict:
    """Try MusicBrainz / iTunes / Deezer for album (primary artist only).
    If still missing/invalid and title exists, fall back to title.
    """
    try:
        meta = enrich(meta)
    except Exception as e:
        print(f"enrich failed for {meta.get('file')}: {e}")
    title = (meta.get("title") or "").strip()
    album = (meta.get("album") or "").strip()
    if _is_invalid_album(album) and title:
        meta["album"] = title
    return meta


def _is_invalid_album(name: str) -> bool:
    """True if the album name is empty, generic, or pure symbols/emoji."""
    if not name:
        return True
    n = name.strip().lower()
    if n in INVALID_ALBUMS:
        return True
    if _JUNK_ALBUM_RE.match(name.strip()):
        return True
    if len(n) <= 1 and not n.isalnum():
        return True
    return False


def _clean_artist(name: str) -> str:
    """Strip common channel/uploader suffixes that aren't real artists."""
    if not name:
        return name
    n = name.strip()
    low = n.lower()
    if low.endswith(" - topic"):
        n = n[:-8].strip()
        low = n.lower()
    for noise in CHANNEL_NOISE:
        if low.endswith(" " + noise) or low.endswith("-" + noise) or low.endswith("_" + noise):
            n = n[: -(len(noise) + 1)].strip(" -_")
            break
    return n or name


def _cleanup_staging_sidecars(mp3_src: Path):
    """Remove leftover yt-dlp thumbnail and .info.json files from staging/."""
    for ext in (".jpg", ".jpeg", ".webp", ".png", ".info.json", ".mp3.jpg", ".mp3.webp"):
        cand = mp3_src.with_name(mp3_src.stem + ext)
        if cand.exists():
            cand.unlink(missing_ok=True)


def _folder_fallback(path: Path) -> dict:
    """If file is under Artist/Album/Title.mp3, use folder names as fallback."""
    parent = path.parent.name if path.parent else ""
    grand = path.parent.parent.name if path.parent and path.parent.parent else ""
    skip = {"", ".", "music", "processed", "staging", "unknown"}
    artist = grand if grand.lower() not in skip else ""
    album = parent if parent.lower() not in skip else ""
    return {"folder_artist": artist, "folder_album": album}


def _read_tags(path: Path) -> dict:
    """Read existing title/artist/album from ID3. Empty string if missing."""
    try:
        from mutagen.mp3 import MP3
        audio = MP3(path)
        tags = audio.tags or {}
        def _get(key):
            v = tags.get(key)
            if not v:
                return ""
            return str(v.text[0]) if hasattr(v, "text") and v.text else ""
        return {
            "title": _get("TIT2"),
            "artist": _get("TPE1"),
            "album": _get("TALB"),
            "album_artist": _get("TPE2"),
        }
    except Exception:
        return {"title": "", "artist": "", "album": "", "album_artist": ""}


def _write_tags(
    path: Path,
    title: str,
    artist: str,
    album: str,
    album_artist: str = None,
    featured: list = None,
    year: str = None,
    cover_path: Path = None,
    lyrics: str = None,
    allow_clear: bool = False,
):
    """Write ID3v2.4 tags including embedded cover art (APIC) and release year (TDRC).
    If allow_clear=True, empty strings remove the corresponding text tag instead of writing "Unknown".
    """
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import TIT2, TPE1, TALB, TPE2, TDRC, TXXX, APIC, USLT
        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        def _set_or_clear(key, frame_cls, value):
            if value and str(value).strip():
                tags[key] = frame_cls(encoding=3, text=str(value).strip())
            elif allow_clear:
                tags.delall(key)

        _set_or_clear("TIT2", TIT2, title)
        _set_or_clear("TPE1", TPE1, artist)
        _set_or_clear("TALB", TALB, album)
        aa = album_artist if (album_artist and str(album_artist).strip()) else (artist if artist else "")
        _set_or_clear("TPE2", TPE2, aa)

        if year:
            tags["TDRC"] = TDRC(encoding=3, text=str(year))
        elif allow_clear:
            tags.delall("TDRC")

        if featured:
            tags["TXXX:FEATURED"] = TXXX(encoding=3, desc="FEATURED", text=", ".join(featured))
        elif allow_clear:
            tags.delall("TXXX:FEATURED")

        if cover_path and cover_path.exists():
            tags.delall("APIC")
            tags["APIC"] = APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # Front cover
                desc="Cover",
                data=cover_path.read_bytes(),
            )

        # Embed plain lyrics (USLT). Skip if empty.
        if lyrics and str(lyrics).strip():
            tags.delall("USLT")
            tags["USLT"] = USLT(encoding=3, lang="und", desc="", text=str(lyrics).strip())
        elif allow_clear:
            tags.delall("USLT")

        audio.save()
    except Exception as e:
        print(f"tag write failed for {path.name}: {e}")
