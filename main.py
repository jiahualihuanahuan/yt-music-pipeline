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
  python main.py --lyrics [DIR]               # add lyrics (USLT); default = MUSIC_DIR
  python main.py --dump-tags [DIR]            # print filename + all ID3 tags (default = MUSIC_DIR)
  python main.py --clean-junk [DIR]           # Phase 1: strip YouTube junk
  python main.py --from-filename [DIR]        # Phase 2: set tags from "Artist - Title" filenames
  python main.py --needs-tag [DIR]            # Phase 3: list files that still need work
  python main.py --fix-art [DIR]              # embed good cover art (local or fetch)
  python main.py --organize [DIR]             # move well-tagged files to organized/Artist/Album/
  python main.py --fix-wrong [DIR]            # retag files whose tags disagree with the filename
  python main.py --process [DIR]              # one-pass for mixed folders (recommended)
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
    LLM_BATCH_SIZE, MUSIC_DIR, ORGANIZED_DIR,
)
from search_download import main as run_downloads
from parse_meta import parse_one, apply_llm_batch, finalize_allowlist, parse_all
from musicbrainz import enrich
from lyrics import fetch_lyrics
from dedup import dedup, is_duplicate
from artwork import process_cover, fetch_cover_url, download_cover
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
    "未知", "未知音素",
}

# YouTube / useless genres to clear
JUNK_GENRES = {
    "music", "entertainment", "people & blogs", "people and blogs",
    "gaming", "news", "education", "comedy", "howto & style", "film & animation",
}

# TXXX keys that are YouTube/yt-dlp junk (keep FEATURED, MusicBrainz*, etc.)
JUNK_TXXX_PREFIXES = (
    "comment", "description", "synopsis", "purl",
    "compatible_brands", "major_brand", "minor_version",
    "encoder", "handler_name",
)

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


# Filename noise that means this is not a real album name
_ALBUM_NOISE_RE = re.compile(
    r"(official\s*(music\s*)?(video|audio|mv|lyric\s*video)|"
    r"\bmv\b|完整版|官方|高清|歌词版|lyric\s*video|music\s*video|"
    r"片头曲|片尾曲|插曲|主题曲|主題曲|戲劇版|戏剧版)",
    re.I,
)

def _is_invalid_album(name: str) -> bool:
    """True if the album name is empty, generic, pure symbols, or looks like a filename."""
    if not name:
        return True
    n = name.strip().lower()
    if n in INVALID_ALBUMS:
        return True
    if _JUNK_ALBUM_RE.match(name.strip()):
        return True
    if len(n) <= 1 and not n.isalnum():
        return True
    # Filename-like: contains video/MV noise, or is very long
    if _ALBUM_NOISE_RE.search(name):
        return True
    if len(n) > 60:
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



def _has_musicbrainz(path: Path) -> bool:
    """True if the file has a MusicBrainz Artist Id (usually means it was already enriched)."""
    try:
        from mutagen.mp3 import MP3
        audio = MP3(path)
        if not audio.tags:
            return False
        for key in audio.tags.keys():
            if key.startswith("TXXX:MusicBrainz") or key.startswith("UFID:http://musicbrainz.org"):
                return True
    except Exception:
        pass
    return False


def _parse_filename_artist_title(stem: str) -> tuple[str, str] | None:
    """Return (artist, title) if the stem looks like 'Artist - Title'.

    Requires a clear separator and both sides non-empty and not too short.
    """
    # Prefer the last " - " to handle "A - B - Title"
    if " - " not in stem:
        return None
    left, right = stem.rsplit(" - ", 1)
    left, right = left.strip(), right.strip()
    if not left or not right or len(left) < 1 or len(right) < 1:
        return None
    # Reject if either side is pure noise
    if left.lower() in INVALID_ARTISTS or right.lower() in INVALID_ALBUMS:
        return None
    if _ALBUM_NOISE_RE.search(left) or _ALBUM_NOISE_RE.search(right):
        return None
    return left, right


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


def organize_one(meta):
    """Move one staged file into Processed/Artist/Album/<original_youtube_name>.mp3
    Filename is never renamed. Only folder and ID3 tags change.
    """
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    src = STAGING_DIR / meta["file"]
    if not src.exists():
        return None

    title = meta.get("title") or src.stem
    artist = _clean_artist(meta.get("artist_canonical") or meta.get("artist") or "Unknown")
    album = meta.get("album") or meta.get("folder_album") or title
    if _is_invalid_album(album):
        album = title
    album_artist = meta.get("album_artist") or artist

    meta["title"] = title
    meta["artist_canonical"] = artist
    meta["album"] = album
    meta["album_artist"] = album_artist

    safe = lambda s: "".join(c for c in s if c not in '/\\:*?"<>|').strip() or "Unknown"
    dest_dir = PROCESSED_DIR / safe(artist) / safe(album)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Strictly preserve original YouTube filename
    dest = dest_dir / src.name
    if src.resolve() != dest.resolve():
        shutil.move(str(src), str(dest))

    # Process cover art before cleaning up staging sidecars
    thumb = src.with_suffix(".jpg")
    if not thumb.exists():
        thumb = Path(str(src) + ".jpg")
    cover_dst = dest_dir / "cover.jpg"
    if thumb.exists():
        process_cover(thumb, cover_dst)

    _cleanup_staging_sidecars(src)

    lyrics_text = None
    try:
        lyrics_text = fetch_lyrics(title, artist, album)
    except Exception as e:
        print(f"lyrics failed for {dest.name}: {e}")

    _write_tags(
        dest,
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        featured=meta.get("featured"),
        year=meta.get("album_year"),
        cover_path=cover_dst if cover_dst.exists() else None,
        lyrics=lyrics_text,
    )

    meta["processed_path"] = str(dest)
    sidecar = dest.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "raw_youtube_title": meta.get("raw"),
        "original_filename": src.name,
        "title": title,
        "artist": meta.get("artist"),
        "featured": meta.get("featured"),
        "artist_canonical": artist,
        "album": album,
        "album_artist": album_artist,
        "album_year": meta.get("album_year"),
        "confidence": meta.get("confidence"),
        "llm_notes": meta.get("llm_notes"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def _process_ready(meta, kept, organized, lock):
    """Enrich + incremental dedup + organize one ready file."""
    try:
        meta = enrich(meta)
    except Exception as e:
        print(f"enrich failed for {meta.get('file')}: {e}")

    if _is_invalid_album(meta.get("album")):
        meta["album"] = meta.get("title") or "Unknown"

    with lock:
        if is_duplicate(meta, kept):
            print(f"dedup skip: {meta.get('file')}")
            src = STAGING_DIR / meta["file"]
            if src.exists():
                src.unlink(missing_ok=True)
            _cleanup_staging_sidecars(src)
            return
        kept.append(meta)

    result = organize_one(meta)
    if result:
        with lock:
            organized.append(result)
        print(f"organized: {meta.get('artist_canonical') or meta.get('artist')} - {meta.get('title')}")


def _is_file_ready(mp3_path: Path) -> bool:
    """Ensure yt-dlp has completely finished extracting audio, converting thumbnails,
    and writing metadata before the watcher thread moves the MP3 out of staging/.
    """
    stem = mp3_path.stem
    parent = mp3_path.parent
    # If any yt-dlp temporary or unconverted thumbnail file exists for this stem, wait
    for p in parent.glob(f"{glob.escape(stem)}*"):
        if p.suffix.lower() in (".part", ".ytdl", ".webp", ".webm", ".m4a", ".opus", ".temp"):
            return False
        if ".temp." in p.name:
            return False
    try:
        size1 = mp3_path.stat().st_size
        time.sleep(0.3)
        size2 = mp3_path.stat().st_size
        return size1 == size2 and size1 > 0
    except OSError:
        return False


def _watch_and_process(stop_event: threading.Event, organized: list, lock: threading.Lock):
    seen = set()
    llm_buf = []
    all_parsed = []
    kept = []

    def flush_llm():
        nonlocal llm_buf
        if not llm_buf:
            return
        apply_llm_batch(llm_buf)
        for m in llm_buf:
            _process_ready(m, kept, organized, lock)
        llm_buf = []

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    # Keep running while downloads are active OR while unseen MP3s remain in staging
    while not stop_event.is_set() or any(f.name not in seen for f in STAGING_DIR.glob("*.mp3")):
        new_files = []
        for f in STAGING_DIR.glob("*.mp3"):
            if f.name not in seen and _is_file_ready(f):
                new_files.append(f)

        for f in new_files:
            seen.add(f.name)
            meta = parse_one(f)
            # MusicBrainz album first when we have title+artist
            if meta.get("title") and meta.get("artist"):
                meta = _enrich_album_first(meta)
            all_parsed.append(meta)
            print(f"parsed: {f.name}  album={meta.get('album')}  needs_llm={meta.get('needs_llm')}")
            if meta.get("needs_llm"):
                llm_buf.append(meta)
                if len(llm_buf) >= LLM_BATCH_SIZE:
                    flush_llm()
            else:
                _process_ready(meta, kept, organized, lock)

        time.sleep(1.5 if not stop_event.is_set() else 0.5)

    flush_llm()
    finalize_allowlist(all_parsed)

    # Clean up any orphaned thumbnails/json from failed downloads where no .mp3 exists
    for sidecar in STAGING_DIR.iterdir():
        if sidecar.suffix.lower() in (".jpg", ".jpeg", ".webp", ".png", ".json", ".part", ".ytdl"):
            mp3_sibling = sidecar.with_suffix(".mp3")
            if not mp3_sibling.exists():
                sidecar.unlink(missing_ok=True)

def run_parallel(target_url: str | None = None):
    stop_event = threading.Event()
    organized = []
    lock = threading.Lock()

    worker = threading.Thread(
        target=_watch_and_process,
        args=(stop_event, organized, lock),
        daemon=True,
    )
    worker.start()

    try:
        if target_url:
            download_url(target_url)
        else:
            run_downloads()
    finally:
        stop_event.set()

    worker.join(timeout=900)
    return organized


def _folder_fallback(path: Path) -> dict:
    """Extract fallback artist/album from folder structure (Artist/Album/file.mp3 or Artist/file.mp3)."""
    skip = {"", ".", "music", "processed", "staging", "unknown", "youtube", "downloads"}
    parent = path.parent.name.strip() if path.parent else ""
    grand = path.parent.parent.name.strip() if path.parent and path.parent.parent else ""

    # If structure is Artist/Album/Track.mp3
    if grand.lower() not in skip and parent.lower() not in skip:
        return {"folder_artist": grand, "folder_album": parent}
    # If structure is Artist/Track.mp3
    if parent.lower() not in skip:
        return {"folder_artist": parent, "folder_album": ""}
    return {"folder_artist": "", "folder_album": ""}


def _retag_one(meta: dict):
    """Enrich and write all ID3 tags in place without renaming or moving the file.
    When meta["llm_authoritative"] is True, empty fields from the LLM are respected
    (tags are cleared instead of filled with fallbacks).
    Album always falls back to title when nothing else is available.
    Skip writing if the LLM call failed (avoid garbage fallback tags).
    """
    if meta.get("llm_notes") == "llm_failed":
        print(f"skip (LLM failed): {meta.get('file') or meta.get('path')}")
        return
    path = Path(meta.get("path") or "")
    if not path.exists():
        return

    authoritative = bool(meta.get("llm_authoritative"))

    try:
        meta = enrich(meta)
    except Exception as e:
        print(f"enrich failed for {path.name}: {e}")

    if authoritative:
        # Title and artist may stay empty (LLM not confident).
        # Album: use only a clean real album; otherwise always copy title.
        from parse_meta import _clean_text, _artist_is_known
        title = _clean_text(meta.get("title"), "title") or ""
        artist = _clean_text(_clean_artist(meta.get("artist") or ""), "artist") or ""
        if artist and not _artist_is_known(artist):
            print(f"  artist not in DB/MusicBrainz, cleared: {artist!r}")
            artist = ""
        album = _clean_text(meta.get("album"), "album") or ""
        # Prefer title whenever album is missing, noisy, or equals the raw filename
        raw = (meta.get("raw") or "").strip()
        if _is_invalid_album(album) or (raw and album == raw) or (title and album == title):
            album = title
        album_artist = artist
        allow_clear = True
    else:
        from parse_meta import _clean_text, _artist_is_known
        # 1. Clean Title (fallback to filename stem)
        title = _clean_text(meta.get("title") or path.stem, "title") or ""

        # 2. Clean Artist (fallback to parent folder if missing or 'Unknown')
        raw_artist = _clean_text(_clean_artist(meta.get("artist_canonical") or meta.get("artist") or ""), "artist") or ""
        if raw_artist and not _artist_is_known(raw_artist):
            print(f"  artist not in DB/MusicBrainz, cleared: {raw_artist!r}")
            raw_artist = ""
        if raw_artist.lower() in INVALID_ARTISTS:
            artist = _clean_artist(meta.get("folder_artist") or "Unknown")
        else:
            artist = raw_artist

        # 3. Clean Album: prefer real album, else folder, else title
        raw_album = (meta.get("album") or "").strip()
        folder_al = (meta.get("folder_album") or "").strip()
        if not _is_invalid_album(raw_album) and raw_album.lower() != artist.lower():
            album = raw_album
        elif not _is_invalid_album(folder_al) and folder_al.lower() != artist.lower():
            album = folder_al
        else:
            album = title

        album_artist = (meta.get("album_artist") or "").strip()
        if album_artist.lower() in INVALID_ARTISTS:
            album_artist = artist
        allow_clear = False

    # Embed folder cover.jpg if present
    cover_path = path.parent / "cover.jpg"

    # Fetch lyrics when we have title+artist (primary artist only)
    lyrics_text = None
    if title and artist:
        try:
            lyrics_text = fetch_lyrics(title, artist, album)
        except Exception as e:
            print(f"lyrics failed for {path.name}: {e}")

    _write_tags(
        path,
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        featured=meta.get("featured"),
        year=meta.get("album_year"),
        cover_path=cover_path if cover_path.exists() else None,
        lyrics=lyrics_text,
        allow_clear=allow_clear,
    )
    album_disp = album or "(cleared)"
    artist_disp = artist or "(cleared)"
    title_disp = title or "(cleared)"
    print(f"tagged: {artist_disp} - {title_disp}  [Album: {album_disp}]")

    sidecar = path.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "raw_youtube_title": meta.get("raw"),
        "original_filename": path.name,
        "title": title,
        "artist": meta.get("artist"),
        "featured": meta.get("featured"),
        "artist_canonical": artist,
        "album": album,
        "album_artist": album_artist,
        "album_year": meta.get("album_year"),
        "confidence": meta.get("confidence"),
        "llm_notes": meta.get("llm_notes"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def reprocess_folder(folder: Path, only_missing: bool = False):
    """Scan all MP3s in a folder recursively and clean/populate all tags using pipeline logic."""
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return

    print(f"scanning for MP3s under {folder} ...")
    all_mp3s = []
    for i, f in enumerate(folder.rglob("*.mp3"), 1):
        if f.name.startswith("."):
            continue
        all_mp3s.append(f)
        if i % 500 == 0:
            print(f"  scanned {i} files...")
    all_mp3s.sort()
    print(f"found {len(all_mp3s)} MP3s")

    if only_missing:
        mp3s = []
        for i, f in enumerate(all_mp3s, 1):
            # Skip files that already have MusicBrainz + valid artist/title
            if _has_musicbrainz(f):
                tags = _read_tags(f)
                if tags["artist"].strip() and tags["title"].strip() and tags["artist"].strip().lower() not in INVALID_ARTISTS:
                    continue
            tags = _read_tags(f)
            has_valid_artist = tags["artist"].strip().lower() not in INVALID_ARTISTS
            has_valid_album = not _is_invalid_album(tags["album"])
            has_valid_title = bool(tags["title"].strip())
            if not (has_valid_title and has_valid_artist and has_valid_album):
                mp3s.append(f)
            if i % 200 == 0:
                print(f"  checked tags on {i}/{len(all_mp3s)} ... ({len(mp3s)} missing so far)")
        print(f"fill-missing: {len(mp3s)} / {len(all_mp3s)} files need tag cleanup")
    else:
        mp3s = all_mp3s
        print(f"reprocess: cleaning all {len(mp3s)} files under {folder}")

    llm_buf = []
    results = []

    def flush():
        nonlocal llm_buf
        if not llm_buf:
            return
        apply_llm_batch(llm_buf)
        for m in llm_buf:
            _retag_one(m)
            results.append(m)
        llm_buf = []

    for f in mp3s:
        meta = parse_one(f)
        meta["path"] = str(f)
        fb = _folder_fallback(f)
        meta["folder_artist"] = fb["folder_artist"]
        meta["folder_album"] = fb["folder_album"]

        # MusicBrainz / iTunes / Deezer for album first (primary artist only)
        meta = _enrich_album_first(meta)

        print(f"parsed: {f.name}  artist={meta.get('artist')}  album={meta.get('album')}  needs_llm={meta.get('needs_llm')}")
        # LLM only if title/artist still need help
        if meta.get("needs_llm") or not meta.get("title") or not meta.get("artist"):
            llm_buf.append(meta)
            if len(llm_buf) >= LLM_BATCH_SIZE:
                flush()
        else:
            _retag_one(meta)
            results.append(meta)

    flush()
    print(f"LLM-only: skipped {skipped_mb} files that already have MusicBrainz + valid tags")
    finalize_allowlist(results)
    print(f"done: cleaned and retagged {len(results)} files")


def llm_only_folder(folder: Path):
    """Force LLM-only inference for every MP3 under folder. No regex parsing. Overwrites tags in place.
    MusicBrainz is tried first for album when title+artist already exist.
    Album falls back to title if nothing else is found.
    """
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return

    print(f"LLM-only mode: scanning {folder} ...")
    all_mp3s = []
    for i, f in enumerate(folder.rglob("*.mp3"), 1):
        if f.name.startswith("."):
            continue
        all_mp3s.append(f)
        if i % 500 == 0:
            print(f"  scanned {i} files...")
    all_mp3s.sort()
    print(f"found {len(all_mp3s)} MP3s — sending all to LLM (no regex, existing tags included)")

    llm_buf = []
    results = []

    def flush():
        nonlocal llm_buf
        if not llm_buf:
            return
        apply_llm_batch(llm_buf)
        for m in llm_buf:
            _retag_one(m)
            results.append(m)
        llm_buf = []

    skipped_mb = 0
    for f in all_mp3s:
        # Skip files that already have MusicBrainz + valid artist/title
        if _has_musicbrainz(f):
            tags = _read_tags(f)
            if tags["artist"].strip() and tags["title"].strip() and tags["artist"].strip().lower() not in INVALID_ARTISTS:
                skipped_mb += 1
                continue
        # Minimal meta: only filename as raw input. Skip regex entirely.
        # Also pass existing tags so the LLM can judge / clear them.
        existing = _read_tags(f)
        meta = {
            "raw": f.stem,
            "file": f.name,
            "path": str(f),
            "needs_llm": True,
            "title": existing.get("title") or "",
            "artist": existing.get("artist") or "",
            "existing_title": existing.get("title") or "",
            "existing_artist": existing.get("artist") or "",
            "existing_album": existing.get("album") or "",
        }
        fb = _folder_fallback(f)
        meta["folder_artist"] = fb["folder_artist"]
        meta["folder_album"] = fb["folder_album"]

        # Optional channel hint
        try:
            from parse_meta import _load_info_and_channel
            _, channel = _load_info_and_channel(f)
            if channel:
                meta["channel_hint"] = channel
        except Exception:
            pass

        # Try MusicBrainz album first when we already have title+artist
        if meta.get("title") and meta.get("artist"):
            meta = _enrich_album_first(meta)

        print(f"queued for LLM: {f.name}")
        llm_buf.append(meta)
        if len(llm_buf) >= LLM_BATCH_SIZE:
            flush()

    flush()
    finalize_allowlist(results)
    print(f"done: LLM-tagged {len(results)} files")


def remove_tags_folder(folder: Path):
    """Strip text ID3 tags from every MP3 under folder. Preserves embedded cover art (APIC)."""
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return

    print(f"remove-tags: scanning {folder} ...")
    all_mp3s = []
    for i, f in enumerate(folder.rglob("*.mp3"), 1):
        if f.name.startswith("."):
            continue
        all_mp3s.append(f)
        if i % 500 == 0:
            print(f"  scanned {i} files...")
    all_mp3s.sort()
    print(f"found {len(all_mp3s)} MP3s — stripping text tags (keeping cover art)")

    stripped = 0
    for f in all_mp3s:
        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import APIC
            audio = MP3(f)
            if audio.tags is None:
                print(f"no tags: {f.name}")
                continue
            # Preserve any existing cover art frames
            covers = [frame for frame in audio.tags.getall("APIC")]
            # Remove everything
            audio.delete()
            # Restore cover art if any
            if covers:
                if audio.tags is None:
                    audio.add_tags()
                for frame in covers:
                    audio.tags.add(frame)
            audio.save()
            stripped += 1
            print(f"stripped: {f.name}  (cover kept)" if covers else f"stripped: {f.name}")
        except Exception as e:
            print(f"failed to strip {f.name}: {e}")

    print(f"done: stripped text tags from {stripped} / {len(all_mp3s)} files (cover art preserved)")



def add_lyrics_folder(folder: Path):
    """Add lyrics (USLT) to MP3s that already have title + artist. Does not change other tags."""
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return

    print(f"lyrics: scanning {folder} ...")
    all_mp3s = []
    for i, f in enumerate(folder.rglob("*.mp3"), 1):
        if f.name.startswith("."):
            continue
        all_mp3s.append(f)
        if i % 500 == 0:
            print(f"  scanned {i} files...")
    all_mp3s.sort()
    print(f"found {len(all_mp3s)} MP3s")

    added = 0
    skipped = 0
    for f in all_mp3s:
        tags = _read_tags(f)
        title = (tags.get("title") or "").strip()
        artist = (tags.get("artist") or "").strip()
        album = (tags.get("album") or "").strip()
        if not title or not artist or artist.lower() in INVALID_ARTISTS:
            print(f"skip (no title/artist): {f.name}")
            skipped += 1
            continue
        # Skip if lyrics already present
        try:
            from mutagen.mp3 import MP3
            audio = MP3(f)
            if audio.tags and audio.tags.getall("USLT"):
                print(f"skip (has lyrics): {f.name}")
                skipped += 1
                continue
        except Exception:
            pass

        lyrics_text = fetch_lyrics(title, artist, album)
        if not lyrics_text:
            print(f"no lyrics: {artist} - {title}")
            skipped += 1
            continue

        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import USLT
            audio = MP3(f)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("USLT")
            audio.tags["USLT"] = USLT(encoding=3, lang="und", desc="", text=lyrics_text)
            audio.save()
            added += 1
            print(f"lyrics: {artist} - {title}")
        except Exception as e:
            print(f"lyrics write failed for {f.name}: {e}")
            skipped += 1

    print(f"done: added lyrics to {added} / {len(all_mp3s)} files ({skipped} skipped)")




def clean_junk_folder(folder: Path):
    """Phase 1: strip YouTube junk TXXX and useless genres. Keep cover, title, artist, album, lyrics, MusicBrainz."""
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return
    print(f"clean-junk: scanning {folder} ...")
    all_mp3s = _list_mp3s(folder)
    all_mp3s.sort()
    print(f"found {len(all_mp3s)} MP3s")

    from mutagen.mp3 import MP3
    cleaned = 0
    for i, f in enumerate(all_mp3s, 1):
        try:
            audio = MP3(f)
            if audio.tags is None:
                continue
            tags = audio.tags
            changed = False

            # Clear junk TXXX
            for key in list(tags.keys()):
                if key.startswith("TXXX:"):
                    desc = key.split(":", 1)[1].lower()
                    if any(desc.startswith(p) or p in desc for p in JUNK_TXXX_PREFIXES):
                        tags.delall(key)
                        changed = True

            # Clear junk genre
            genre = ""
            if "TCON" in tags and tags["TCON"].text:
                genre = str(tags["TCON"].text[0]).strip().lower()
            if genre in JUNK_GENRES:
                tags.delall("TCON")
                changed = True

            if changed:
                audio.save()
                cleaned += 1
        except Exception as e:
            print(f"  clean failed for {f.name}: {e}")
        if i % 1000 == 0:
            print(f"  processed {i}/{len(all_mp3s)} ...")

    print(f"done: cleaned junk on {cleaned} / {len(all_mp3s)} files")



def from_filename_folder(folder: Path):
    """Phase 2: set artist/title from 'Artist - Title.mp3' filenames. Album falls back to title."""
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return
    print(f"from-filename: scanning {folder} ...")
    all_mp3s = _list_mp3s(folder)
    all_mp3s.sort()
    print(f"found {len(all_mp3s)} MP3s")

    from mutagen.mp3 import MP3
    from mutagen.id3 import TIT2, TPE1, TALB, TPE2
    applied = 0
    skipped = 0
    for i, f in enumerate(all_mp3s, 1):
        parsed = _parse_filename_artist_title(f.stem)
        if not parsed:
            skipped += 1
            continue
        artist, title = parsed
        # Skip if already has MusicBrainz and valid tags
        if _has_musicbrainz(f):
            tags = _read_tags(f)
            if tags["artist"].strip() and tags["title"].strip():
                skipped += 1
                continue
        try:
            audio = MP3(f)
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags
            tags["TIT2"] = TIT2(encoding=3, text=title)
            tags["TPE1"] = TPE1(encoding=3, text=artist)
            tags["TPE2"] = TPE2(encoding=3, text=artist)
            # Album = title unless a clean album already exists
            current_album = ""
            if "TALB" in tags and tags["TALB"].text:
                current_album = str(tags["TALB"].text[0]).strip()
            if _is_invalid_album(current_album):
                tags["TALB"] = TALB(encoding=3, text=title)
            audio.save()
            applied += 1
            print(f"  {artist} - {title}")
        except Exception as e:
            print(f"  write failed for {f.name}: {e}")
            skipped += 1
        if i % 1000 == 0:
            print(f"  processed {i}/{len(all_mp3s)} ...")

    print(f"done: applied filename tags to {applied} / {len(all_mp3s)} files ({skipped} skipped)")



def needs_tag_folder(folder: Path):
    """Phase 3: list files that still need tagging (no valid artist/title, or album is filename-like)."""
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return
    print(f"needs-tag: scanning {folder} ...")
    all_mp3s = _list_mp3s(folder)
    all_mp3s.sort()
    print(f"found {len(all_mp3s)} MP3s")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = LOG_DIR / f"needs_tag_{stamp}.txt"
    need = []
    for i, f in enumerate(all_mp3s, 1):
        tags = _read_tags(f)
        artist = tags["artist"].strip()
        title = tags["title"].strip()
        album = tags["album"].strip()
        reasons = []
        if not title or title.lower() in INVALID_ALBUMS:
            reasons.append("no_title")
        if not artist or artist.lower() in INVALID_ARTISTS:
            reasons.append("no_artist")
        if _is_invalid_album(album):
            reasons.append("bad_album")
        if reasons:
            need.append((f, reasons, artist, title, album))
        if i % 2000 == 0:
            print(f"  checked {i}/{len(all_mp3s)} ... ({len(need)} need work)")

    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(f"# needs-tag from {folder}\n")
        fh.write(f"# {len(need)} / {len(all_mp3s)} files need work\n\n")
        for f, reasons, artist, title, album in need:
            fh.write(f"{f}\n")
            fh.write(f"  reasons: {', '.join(reasons)}\n")
            fh.write(f"  artist={artist or '(empty)'}  title={title or '(empty)'}  album={album or '(empty)'}\n\n")

    print(f"done: {len(need)} / {len(all_mp3s)} files need tagging")
    print(f"wrote {out_path}")



def _apic_is_good(frame) -> bool:
    """True if embedded cover is large enough to keep."""
    try:
        data = frame.data
        if not data or len(data) < 8000:
            return False
        # Optional size check via PIL
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(data))
        w, h = im.size
        return w >= 200 and h >= 200
    except Exception:
        return False


def _find_local_cover(mp3: Path) -> Path | None:
    """Look for a cover image next to the file or in the parent folder."""
    candidates = [
        mp3.parent / "cover.jpg",
        mp3.parent / "folder.jpg",
        mp3.parent / "Folder.jpg",
        mp3.with_suffix(".jpg"),
        mp3.with_name(mp3.stem + ".jpg"),
    ]
    for c in candidates:
        if c.exists() and c.stat().st_size > 2000:
            return c
    # Any jpg in the same folder (prefer largest)
    jpgs = [p for p in mp3.parent.glob("*.jpg") if p.stat().st_size > 2000]
    if jpgs:
        return max(jpgs, key=lambda p: p.stat().st_size)
    return None


def fix_art_folder(folder: Path):
    """Embed a good cover for every MP3.

    Priority:
    1. Keep existing APIC if large enough
    2. Nearby cover.jpg / folder.jpg / *.jpg
    3. Fetch from iTunes/Deezer when title+artist are known
    """
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return
    print(f"fix-art: scanning {folder} ...")
    all_mp3s = _list_mp3s(folder)
    all_mp3s.sort()
    print(f"found {len(all_mp3s)} MP3s")

    from mutagen.mp3 import MP3
    from mutagen.id3 import APIC
    import tempfile

    kept = 0
    embedded = 0
    fetched = 0
    skipped = 0

    for i, f in enumerate(all_mp3s, 1):
        try:
            audio = MP3(f)
            if audio.tags is None:
                audio.add_tags()
            tags = audio.tags

            # 1. Keep good existing cover
            good = False
            for frame in tags.getall("APIC"):
                if _apic_is_good(frame):
                    good = True
                    break
            if good:
                kept += 1
                continue

            # 2. Local image
            src = _find_local_cover(f)
            if src:
                with tempfile.TemporaryDirectory() as td:
                    dst = Path(td) / "cover.jpg"
                    if process_cover(src, dst) and dst.exists():
                        tags.delall("APIC")
                        tags["APIC"] = APIC(
                            encoding=3,
                            mime="image/jpeg",
                            type=3,
                            desc="Cover",
                            data=dst.read_bytes(),
                        )
                        audio.save()
                        embedded += 1
                        print(f"  embedded local: {f.name}")
                        continue

            # 3. Fetch when we have title+artist
            tags_dict = _read_tags(f)
            title = tags_dict["title"].strip()
            artist = tags_dict["artist"].strip()
            if title and artist and artist.lower() not in INVALID_ARTISTS:
                url = fetch_cover_url(title, artist)
                if url:
                    with tempfile.TemporaryDirectory() as td:
                        raw = Path(td) / "raw.jpg"
                        dst = Path(td) / "cover.jpg"
                        if download_cover(url, raw) and process_cover(raw, dst) and dst.exists():
                            tags.delall("APIC")
                            tags["APIC"] = APIC(
                                encoding=3,
                                mime="image/jpeg",
                                type=3,
                                desc="Cover",
                                data=dst.read_bytes(),
                            )
                            audio.save()
                            fetched += 1
                            print(f"  fetched: {artist} - {title}")
                            continue

            skipped += 1
        except Exception as e:
            print(f"  art failed for {f.name}: {e}")
            skipped += 1
        if i % 500 == 0:
            print(f"  processed {i}/{len(all_mp3s)} (kept={kept} local={embedded} fetched={fetched})")

    print(f"done: kept={kept} embedded_local={embedded} fetched={fetched} skipped={skipped} / {len(all_mp3s)}")



def _is_under_organized(path: Path) -> bool:
    """True if path is inside ORGANIZED_DIR."""
    try:
        path.resolve().relative_to(ORGANIZED_DIR.resolve())
        return True
    except ValueError:
        return False


def _list_mp3s(folder: Path) -> list[Path]:
    """List MP3s under folder, skipping the organized library and hidden files."""
    out = []
    for f in folder.rglob("*.mp3"):
        if f.name.startswith("."):
            continue
        if _is_under_organized(f):
            continue
        out.append(f)
    out.sort()
    return out


def _is_well_tagged(tags: dict) -> bool:
    """True if title, artist, and album are all valid."""
    title = (tags.get("title") or "").strip()
    artist = (tags.get("artist") or "").strip()
    album = (tags.get("album") or "").strip()
    if not title or title.lower() in INVALID_ALBUMS:
        return False
    if not artist or artist.lower() in INVALID_ARTISTS:
        return False
    if _is_invalid_album(album):
        return False
    return True


def _safe_name(s: str) -> str:
    return "".join(c for c in s if c not in '/\\:*?"<>|').strip() or "Unknown"


def _file_hash(path: Path, block=1024 * 1024) -> str:
    """MD5 of file content (for exact dedup)."""
    import hashlib
    h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(block)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _remove_empty_dirs(root: Path) -> int:
    """Remove empty directories under root (bottom-up)."""
    removed = 0
    for dirpath in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not dirpath.is_dir() or dirpath == root:
            continue
        try:
            if not any(dirpath.iterdir()):
                dirpath.rmdir()
                removed += 1
        except OSError:
            pass
    return removed


def organize_library(folder: Path):
    """Move well-tagged files into ORGANIZED_DIR/Artist/Album/filename.mp3.

    - Deduplicates by content hash (keeps existing organized copy)
    - Removes empty folders after the move
    - Does not touch files already under ORGANIZED_DIR
    """
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return

    ORGANIZED_DIR.mkdir(parents=True, exist_ok=True)
    print(f"organize: scanning {folder} (organized = {ORGANIZED_DIR}) ...")
    all_mp3s = _list_mp3s(folder)
    print(f"found {len(all_mp3s)} MP3s outside organized/")

    existing_hashes = {}
    for f in ORGANIZED_DIR.rglob("*.mp3"):
        try:
            existing_hashes[_file_hash(f)] = f
        except Exception:
            pass
    print(f"indexed {len(existing_hashes)} existing organized files")

    moved = 0
    dupes = 0
    skipped = 0
    seen_hashes = dict(existing_hashes)

    for i, f in enumerate(all_mp3s, 1):
        tags = _read_tags(f)
        if not _is_well_tagged(tags):
            skipped += 1
            continue

        title = tags["title"].strip()
        artist = _clean_artist(tags["artist"]).strip()
        album = tags["album"].strip()
        if _is_invalid_album(album):
            album = title

        try:
            h = _file_hash(f)
        except Exception as e:
            print(f"  hash failed for {f.name}: {e}")
            skipped += 1
            continue

        if h in seen_hashes:
            try:
                f.unlink()
                dupes += 1
                print(f"  dupe removed: {f.name}")
            except Exception as e:
                print(f"  dupe remove failed for {f.name}: {e}")
            continue

        dest_dir = ORGANIZED_DIR / _safe_name(artist) / _safe_name(album)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f.name
        if dest.exists():
            dest = dest_dir / f"{f.stem}_{h[:6]}{f.suffix}"

        try:
            import shutil
            shutil.move(str(f), str(dest))
            seen_hashes[h] = dest
            moved += 1
            print(f"  moved: {artist} / {album} / {f.name}")
        except Exception as e:
            print(f"  move failed for {f.name}: {e}")
            skipped += 1

        if i % 500 == 0:
            print(f"  processed {i}/{len(all_mp3s)} (moved={moved} dupes={dupes})")

    empty_src = _remove_empty_dirs(folder)
    empty_org = _remove_empty_dirs(ORGANIZED_DIR)
    print(f"done: moved={moved} dupes_removed={dupes} skipped={skipped} empty_dirs_removed={empty_src + empty_org}")



def _tags_look_wrong(path: Path, tags: dict) -> list[str]:
    """Return reasons if existing tags disagree with the filename or look noisy."""
    reasons = []
    title = (tags.get("title") or "").strip()
    artist = (tags.get("artist") or "").strip()
    album = (tags.get("album") or "").strip()

    parsed = _parse_filename_artist_title(path.stem)
    if parsed:
        fn_artist, fn_title = parsed
        # Rough mismatch: neither contains the other
        if title and fn_title.lower() not in title.lower() and title.lower() not in fn_title.lower():
            reasons.append("title_mismatch")
        if artist and fn_artist.lower() not in artist.lower() and artist.lower() not in fn_artist.lower():
            reasons.append("artist_mismatch")

    # Album looks like a filename / video title
    if album and _is_invalid_album(album):
        reasons.append("bad_album")

    # Artist looks like a channel / uploader
    if artist and artist.lower() in INVALID_ARTISTS:
        reasons.append("bad_artist")

    return reasons


def fix_wrong_folder(folder: Path):
    """Find files whose tags disagree with the filename (or are noisy) and retag them with LLM."""
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return

    print(f"fix-wrong: scanning {folder} (skipping organized/) ...")
    all_mp3s = _list_mp3s(folder)
    print(f"found {len(all_mp3s)} MP3s")

    wrong = []
    for i, f in enumerate(all_mp3s, 1):
        tags = _read_tags(f)
        reasons = _tags_look_wrong(f, tags)
        if reasons:
            wrong.append((f, reasons, tags))
        if i % 2000 == 0:
            print(f"  checked {i}/{len(all_mp3s)} ... ({len(wrong)} look wrong)")

    print(f"fix-wrong: {len(wrong)} / {len(all_mp3s)} files look wrong")
    if not wrong:
        return

    # Queue them for LLM-only overwrite
    llm_buf = []
    results = []

    def flush():
        nonlocal llm_buf
        if not llm_buf:
            return
        apply_llm_batch(llm_buf)
        for m in llm_buf:
            _retag_one(m)
            results.append(m)
        llm_buf = []

    for f, reasons, tags in wrong:
        meta = {
            "raw": f.stem,
            "file": f.name,
            "path": str(f),
            "needs_llm": True,
            "title": tags.get("title") or "",
            "artist": tags.get("artist") or "",
            "existing_title": tags.get("title") or "",
            "existing_artist": tags.get("artist") or "",
            "existing_album": tags.get("album") or "",
        }
        fb = _folder_fallback(f)
        meta["folder_artist"] = fb["folder_artist"]
        meta["folder_album"] = fb["folder_album"]
        print(f"  wrong ({', '.join(reasons)}): {f.name}")
        llm_buf.append(meta)
        if len(llm_buf) >= LLM_BATCH_SIZE:
            flush()

    flush()
    finalize_allowlist(results)
    print(f"done: retagged {len(results)} wrong files")



def process_folder(folder: Path):
    """One-pass for a mixed library.

    1. Strip YouTube junk tags
    2. Apply safe filename artist/title where it matches
    3. LLM-retag only files that are missing tags or look wrong
    4. Leave well-tagged files alone
    Skips the organized/ folder.
    """
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return

    print(f"process: {folder} (skipping organized/)")

    print("\n--- step 1: clean junk ---")
    clean_junk_folder(folder)

    print("\n--- step 2: from filename ---")
    from_filename_folder(folder)

    print("\n--- step 3: collect missing + wrong ---")
    all_mp3s = _list_mp3s(folder)
    need = []
    for f in all_mp3s:
        tags = _read_tags(f)
        reasons = []
        if not _is_well_tagged(tags):
            reasons.append("missing")
        reasons.extend(_tags_look_wrong(f, tags))
        if reasons:
            need.append((f, reasons, tags))

    print(f"process: {len(need)} / {len(all_mp3s)} files need LLM")
    if not need:
        print("nothing to do")
        return

    print("\n--- step 4: LLM on remaining ---")
    llm_buf = []
    results = []

    def flush():
        nonlocal llm_buf
        if not llm_buf:
            return
        apply_llm_batch(llm_buf)
        for m in llm_buf:
            _retag_one(m)
            results.append(m)
        llm_buf = []

    for f, reasons, tags in need:
        meta = {
            "raw": f.stem,
            "file": f.name,
            "path": str(f),
            "needs_llm": True,
            "title": tags.get("title") or "",
            "artist": tags.get("artist") or "",
            "existing_title": tags.get("title") or "",
            "existing_artist": tags.get("artist") or "",
            "existing_album": tags.get("album") or "",
        }
        fb = _folder_fallback(f)
        meta["folder_artist"] = fb["folder_artist"]
        meta["folder_album"] = fb["folder_album"]
        print(f"  need ({', '.join(reasons)}): {f.name}")
        llm_buf.append(meta)
        if len(llm_buf) >= LLM_BATCH_SIZE:
            flush()

    flush()
    finalize_allowlist(results)
    print(f"\ndone: processed {len(results)} files")
    print("next: python main.py --fix-art   then   python main.py --organize")


def dump_tags_folder(folder: Path):
    """Write filename and all ID3 tags for every MP3 under folder to a file."""
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return

    print(f"dump-tags: scanning {folder} ...")
    all_mp3s = []
    for i, f in enumerate(folder.rglob("*.mp3"), 1):
        if f.name.startswith("."):
            continue
        all_mp3s.append(f)
        if i % 1000 == 0:
            print(f"  scanned {i} files...")
    all_mp3s.sort()
    print(f"found {len(all_mp3s)} MP3s")

    try:
        from mutagen.mp3 import MP3
    except ImportError:
        print("mutagen not installed")
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = LOG_DIR / f"dump_tags_{stamp}.txt"
    print(f"writing {out_path} ...")

    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(f"# dump-tags from {folder}\n")
        fh.write(f"# {len(all_mp3s)} files\n\n")
        for i, f in enumerate(all_mp3s, 1):
            try:
                audio = MP3(f)
                tags = audio.tags
                fh.write(f"FILE: {f}\n")
                if not tags:
                    fh.write("  (no tags)\n\n")
                    continue
                for key in sorted(tags.keys()):
                    frame = tags[key]
                    if hasattr(frame, "text") and frame.text:
                        val = "; ".join(str(x) for x in frame.text)
                    else:
                        val = str(frame)
                    if len(val) > 200:
                        val = val[:200] + "..."
                    fh.write(f"  {key}: {val}\n")
                fh.write("\n")
            except Exception as e:
                fh.write(f"FILE: {f}\n")
                fh.write(f"  ERROR: {e}\n\n")
            if i % 500 == 0:
                print(f"  wrote {i}/{len(all_mp3s)} ...")

    print(f"done: wrote {len(all_mp3s)} files to {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-dl", action="store_true")
    ap.add_argument("--reprocess", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"reparse + retag all MP3s under DIR (default: {MUSIC_DIR})")
    ap.add_argument("--fill-missing", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"only retag MP3s missing title, artist, or album (default: {MUSIC_DIR})")
    ap.add_argument("--llm-only", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"force LLM inference only (default: {MUSIC_DIR})")
    ap.add_argument("--remove-tags", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"strip text ID3 tags (keep cover art) (default: {MUSIC_DIR})")
    ap.add_argument("--lyrics", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"add lyrics (USLT) for MP3s with title+artist (default: {MUSIC_DIR})")
    ap.add_argument("--dump-tags", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"print filename + all ID3 tags (default: {MUSIC_DIR})")
    ap.add_argument("--clean-junk", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"Phase 1: strip YouTube junk TXXX and useless genres (default: {MUSIC_DIR})")
    ap.add_argument("--from-filename", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"Phase 2: set artist/title from 'Artist - Title' filenames (default: {MUSIC_DIR})")
    ap.add_argument("--needs-tag", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"Phase 3: list files that still need tagging (default: {MUSIC_DIR})")
    ap.add_argument("--fix-art", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"embed good cover art (keep existing, local image, or fetch) (default: {MUSIC_DIR})")
    ap.add_argument("--organize", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"move well-tagged files to {ORGANIZED_DIR}/Artist/Album/ (dedupe, clean empty dirs)")
    ap.add_argument("--fix-wrong", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help="retag files whose tags disagree with the filename or look noisy")
    ap.add_argument("--process", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help="one-pass for mixed folders: clean junk, from-filename, LLM only missing+wrong")
    ap.add_argument("--url", "--playlist", dest="url", type=str, metavar="URL",
                help="download and process only a specific YouTube playlist or video URL")
    args = ap.parse_args()

    # Always try to init the local DB (path is now platform-safe)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"WARNING: could not create LOG_DIR {LOG_DIR}: {e}")
    try:
        init_db()
        print("local DB stats:", stats())
    except Exception as e:
        print(f"WARNING: could not init local DB: {e}")

    if args.dump_tags:
        dump_tags_folder(Path(args.dump_tags))
        return
    if args.clean_junk:
        clean_junk_folder(Path(args.clean_junk))
        return
    if args.from_filename:
        from_filename_folder(Path(args.from_filename))
        return
    if args.needs_tag:
        needs_tag_folder(Path(args.needs_tag))
        return
    if args.fix_art:
        fix_art_folder(Path(args.fix_art))
        return
    if args.organize:
        organize_library(Path(args.organize))
        return
    if args.fix_wrong:
        fix_wrong_folder(Path(args.fix_wrong))
        return
    if args.process:
        process_folder(Path(args.process))
        return
    if args.llm_only:
        llm_only_folder(Path(args.llm_only))
        return
    if args.remove_tags:
        remove_tags_folder(Path(args.remove_tags))
        return
    if args.lyrics:
        add_lyrics_folder(Path(args.lyrics))
        return
    if args.fill_missing:
        reprocess_folder(Path(args.fill_missing), only_missing=True)
        return
    if args.reprocess:
        reprocess_folder(Path(args.reprocess), only_missing=False)
        return

    if args.dry_run:
        import config as cfg
        cfg.DRY_RUN = True

    if args.skip_dl or args.dry_run:
        results = parse_all()
        enriched = []
        for meta in results:
            try:
                enriched.append(enrich(meta))
            except Exception as e:
                print(f"enrich failed for {meta.get('file')}: {e}")
                enriched.append(meta)
        kept = dedup(enriched)
        organized = []
        for m in kept:
            r = organize_one(m)
            if r:
                organized.append(r)
    else:
        organized = run_parallel(target_url=args.url)

    summary = {
        "run_at": datetime.now().isoformat(),
        "organized": len(organized),
        "high_confidence": sum(1 for m in organized if m.get("confidence") == "high"),
        "llm_used": sum(1 for m in organized if m.get("llm_notes") is not None),
    }
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}.json").write_text(
            json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


if __name__ == "__main__":
    main()
