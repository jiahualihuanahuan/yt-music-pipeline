#!/usr/bin/env python3
"""Music pipeline orchestrator.

Works only on MP3s that are already on disk (no YouTube search/download).

Usage:
  python main.py /path/to/music               # recommended: same as --process DIR
  python main.py                              # same, on ~/Music (C:\\Users\\<name>\\Music)
  python main.py DIR --reprocess              # full re-run, overwrite tags/lyrics/art
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
  python main.py --process [DIR]              # one-pass: clean, retag, fix-art, organize (recommended)
  python main.py --process [DIR] --no-fix-art --no-organize  # retag only
"""
import argparse
import json
import shutil
import re
import unicodedata
from pathlib import Path
from datetime import datetime
from config import (
    LOG_DIR,
    LLM_BATCH_SIZE, MUSIC_DIR, ORGANIZED_DIR, BASE_DIR,
)
from parse_meta import parse_one, apply_llm_batch, finalize_allowlist
from musicbrainz import enrich
from lyrics import fetch_lyrics
from artwork import process_cover, fetch_cover_url, download_cover
from local_db import init_db, stats

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
    # every YouTube video category (yt-dlp writes these into TCON)
    "music", "entertainment", "people & blogs", "people and blogs",
    "gaming", "news", "news & politics", "education", "comedy",
    "howto & style", "film & animation", "travel & events", "sports",
    "science & technology", "pets & animals", "autos & vehicles",
    "nonprofits & activism", "shows", "trailers", "movies",
}

# Artist tags that are really the YouTube uploader / channel, not a performer
_UPLOADER_RE = re.compile(
    r"(vevo$|\s-\stopic$|^uploads from |official|channel|records$|music$|\btv$|lyrics?$|"
    r"^disney|^\w*\d{3,}$|^[a-z]+[0-9]+[a-z0-9]*$|stories with|kids|playlist)",
    re.I,
)

# Title junk left by lyric-video channels / YouTube
_TITLE_JUNK_RE = re.compile(
    r"(『|』|【|】|official\s+(music\s+)?(video|audio|mv)|\[high quality\]|\bhd\b|"
    r"with lyrics|sing-a-long|動態歌詞|动态歌词|完整版|\[[A-Za-z0-9_-]{11}\])",
    re.I,
)

NON_MUSIC_TAG = "PIPELINE_NON_MUSIC"

# Album that is really a YouTube channel tab / playlist ("X - Videos", "Mix - ...").
# clean-junk replaces it with the title (the single-track album convention).
_CHANNEL_ALBUM_RE = re.compile(r"\s-\s(videos|shorts|topic)$|^(mix|top songs)\s-\s|^uploads from ", re.I)

# Variety-show / contest names that end up in the title tag
_SHOW_RE = re.compile(
    r"^(ride the wind|乘风\s*20\d\d|乘風\s*20\d\d|歌手\s*20\d\d|singer\s*20\d\d|sing!?\s*asia|"
    r"我是歌手.*|中国好声音.*|中國好聲音.*|天赐的声音.*|声生不息.*|时光音乐会.*|eurovision.*|打歌20\d\d.*)$",
    re.I,
)


def _looks_like_uploader(artist: str, path: Path | None = None) -> bool:
    a = (artist or "").strip()
    if not a:
        return False
    if _UPLOADER_RE.search(a):
        return True
    # artist equals a playlist-ish folder name ("Mix - X", "Top Songs - X")
    if path is not None and re.match(r"^(mix|top songs|uploads from)\s*-?\s*", path.parent.name, re.I) \
            and a.lower() == path.parent.name.lower():
        return True
    return False

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
    genre: str = None,
    original_artist: str = None,
):
    """Write ID3v2.4 tags including embedded cover art (APIC) and release year (TDRC).
    If allow_clear=True, empty strings remove the corresponding text tag instead of writing "Unknown".
    """
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import TIT2, TPE1, TALB, TPE2, TDRC, TXXX, APIC, USLT, TCON
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

        if genre and str(genre).strip():
            tags["TCON"] = TCON(encoding=3, text=str(genre).strip())

        import config as _cfgw
        if original_artist and getattr(_cfgw, "WRITE_ORIGINAL_ARTIST", True) and original_artist.strip() != (artist or "").strip():
            tags["TXXX:ORIGINAL_ARTIST"] = TXXX(encoding=3, desc="ORIGINAL_ARTIST", text=original_artist.strip())

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

    if meta.get("is_music") is False:
        _handle_non_music(path, meta)
        return

    authoritative = bool(meta.get("llm_authoritative"))

    try:
        meta = enrich(meta)
    except Exception as e:
        print(f"enrich failed for {path.name}: {e}")

    if authoritative:
        # Title and artist may stay empty (LLM not confident).
        # Album: use only a clean real album; otherwise always copy title.
        from parse_meta import _clean_text
        title = _clean_text(meta.get("title"), "title") or ""
        artist = _clean_text(_clean_artist(meta.get("artist") or ""), "artist") or ""
        album = _clean_text(meta.get("album"), "album") or ""
        # Prefer title whenever album is missing, noisy, or equals the raw filename
        raw = (meta.get("raw") or "").strip()
        if _is_invalid_album(album) or (raw and album == raw) or (title and album == title):
            album = title
        album_artist = artist
        allow_clear = True
    else:
        from parse_meta import _clean_text
        # 1. Clean Title (fallback to filename stem)
        title = _clean_text(meta.get("title") or path.stem, "title") or ""

        # 2. Clean Artist (fallback to parent folder if missing or 'Unknown')
        raw_artist = _clean_text(_clean_artist(meta.get("artist_canonical") or meta.get("artist") or ""), "artist") or ""
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
        genre=meta.get("genre"),
        original_artist=meta.get("original_artist"),
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


def _is_marked_non_music(path: Path) -> bool:
    try:
        from mutagen.id3 import ID3
        return bool(ID3(path).getall(f"TXXX:{NON_MUSIC_TAG}"))
    except Exception:
        return False


def _handle_non_music(path: Path, meta: dict):
    """Never guess tags for talk clips, vlogs, news, podcasts, cartoons...

    NON_MUSIC_ACTION = "skip" (default): mark the file (TXXX:PIPELINE_NON_MUSIC) so
    --process / --organize leave it alone; tags are otherwise untouched.
    NON_MUSIC_ACTION = "move": also move it to NON_MUSIC_DIR (keeps the folder name).
    """
    import config as _cfg
    action = getattr(_cfg, "NON_MUSIC_ACTION", "skip")
    reason = meta.get("llm_notes") or "non-music"
    if action == "delete" and _DELETE_RUN is not None:
        conf = str(meta.get("confidence") or "").lower()
        if _DELETE_RUN.consider(path, f"non-music: {reason}", "high" if conf == "high" else (conf or "low")):
            return
    try:
        from mutagen.id3 import ID3, TXXX, ID3NoHeaderError
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall(f"TXXX:{NON_MUSIC_TAG}")
        tags.add(TXXX(encoding=3, desc=NON_MUSIC_TAG, text=[str(reason)[:200]]))
        tags.save(path)
    except Exception as e:
        print(f"  could not mark non-music {path.name}: {e}")
    if action == "move":
        dest_root = Path(getattr(_cfg, "NON_MUSIC_DIR", MUSIC_DIR / "_non_music"))
        dest = dest_root / path.parent.name / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.move(str(path), str(dest))
            print(f"non-music -> {dest}")
            return
    print(f"non-music (left as is): {path.name}  [{reason}]")


def _apply_rules(meta: dict) -> bool:
    """Fill title/artist from title_rules when it is sure. Returns True if used."""
    import config as _cfg
    if not getattr(_cfg, "USE_TITLE_RULES", True):
        return False
    import title_rules
    r = title_rules.parse(meta.get("raw") or "")
    if not r:
        return False
    meta.update(title=r["title"], artist=r["artist"], featured=r["featured"],
                original_artist=r.get("original_artist"), album=None,
                confidence="rules", needs_llm=False, llm_notes=f"rules:{r['rule']}",
                llm_authoritative=True, is_music=True)
    return True


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

            # Clear comments that are only a YouTube URL (yt-dlp --embed-metadata)
            for key in [k for k in tags.keys() if k.startswith("COMM")]:
                text = " ".join(str(t) for t in (tags[key].text or [])).strip()
                if not text or re.fullmatch(r"https?://(www\.)?(youtube\.com|youtu\.be)\S*", text):
                    tags.delall(key)
                    changed = True

            # Album that is a channel/playlist name -> use the title
            if "TALB" in tags and tags["TALB"].text and "TIT2" in tags and tags["TIT2"].text:
                if _CHANNEL_ALBUM_RE.search(str(tags["TALB"].text[0]).strip()):
                    from mutagen.id3 import TALB
                    tags.setall("TALB", [TALB(encoding=3, text=[str(tags["TIT2"].text[0])])])
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


def fix_art_folder(folder: Path, include_organized: bool = False, keep_existing: bool = False,
                   force: bool = False):
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
    all_mp3s = _list_mp3s(folder, include_organized=include_organized)
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
            if not force and (good or (keep_existing and tags.getall("APIC"))):
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


def _is_under_delete_dir(path: Path) -> bool:
    import config as _cfg
    d = getattr(_cfg, "DELETE_DIR", None)
    if not d:
        return False
    try:
        path.resolve().relative_to(Path(d).resolve())
        return True
    except ValueError:
        return False


# Active DeleteRun for this pipeline run (set by _retag_folder), else None.
_DELETE_RUN = None


def _list_mp3s(folder: Path, include_organized: bool = False) -> list[Path]:
    """List MP3s under folder, skipping the organized library (unless include_organized) and hidden files."""
    out = []
    for f in folder.rglob("*.mp3"):
        if f.name.startswith("."):
            continue
        if (_is_under_organized(f) and not include_organized) or _is_under_delete_dir(f):
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

    # Index existing organized files by size (fast). Full hash only on size collision.
    print("indexing existing organized files by size ...")
    size_index = {}  # size -> list of paths
    n_org = 0
    for f in ORGANIZED_DIR.rglob("*.mp3"):
        try:
            size_index.setdefault(f.stat().st_size, []).append(f)
            n_org += 1
            if n_org % 2000 == 0:
                print(f"  indexed {n_org} ...")
        except OSError:
            pass
    print(f"indexed {n_org} existing organized files")

    import config as _cfg
    import artist_aliases
    aliases = artist_aliases.load(getattr(_cfg, "ARTIST_ALIASES_FILE", BASE_DIR / "artist_aliases.json"))
    moved = 0
    dupes = 0
    skipped = 0

    for i, f in enumerate(all_mp3s, 1):
        tags = _read_tags(f)
        if not _is_well_tagged(tags) or _is_marked_non_music(f):
            skipped += 1
            continue

        title = tags["title"].strip()
        artist = _clean_artist(tags["artist"]).strip()
        album = tags["album"].strip()
        if _is_invalid_album(album):
            album = title
        canon = aliases.get(artist)
        if canon and canon != artist:
            _set_artist(f, canon)
            artist = canon

        # Fast dedup: same size as something already organized -> confirm with hash
        try:
            size = f.stat().st_size
        except OSError:
            skipped += 1
            continue
        is_dupe = False
        for cand in size_index.get(size, []):
            try:
                if _file_hash(f) == _file_hash(cand):
                    is_dupe = True
                    break
            except Exception:
                continue
        if is_dupe:
            try:
                f.unlink()
                f.with_suffix(".lrc").unlink(missing_ok=True)
                dupes += 1
                print(f"  dupe removed: {f.name}")
            except Exception as e:
                print(f"  dupe remove failed for {f.name}: {e}")
            continue

        dest_dir = ORGANIZED_DIR / _safe_name(artist) / _safe_name(album)
        dest_dir.mkdir(parents=True, exist_ok=True)
        import title_rules
        clean_stem = title_rules.VIDEO_ID_RE.sub("", f.stem).strip() or f.stem
        dest = dest_dir / f"{clean_stem}{f.suffix}"
        if dest.exists():
            dest = dest_dir / f"{f.stem}_{size}{f.suffix}"

        try:
            import shutil
            shutil.move(str(f), str(dest))
            lrc = f.with_suffix(".lrc")
            if lrc.exists() and not dest.with_suffix(".lrc").exists():
                shutil.move(str(lrc), str(dest.with_suffix(".lrc")))
            size_index.setdefault(dest.stat().st_size, []).append(dest)
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



def _set_artist(path: Path, artist: str):
    """Rewrite TPE1 (and TPE2 when it held the old artist)."""
    from mutagen.id3 import ID3, TPE1, TPE2
    t = ID3(path)
    old = str(t["TPE1"].text[0]) if "TPE1" in t and t["TPE1"].text else ""
    t.setall("TPE1", [TPE1(encoding=3, text=[artist])])
    if "TPE2" not in t or not t["TPE2"].text or str(t["TPE2"].text[0]) == old:
        t.setall("TPE2", [TPE2(encoding=3, text=[artist])])
    t.save(path)


def build_aliases_folder(folder: Path):
    """Propose {variant: canonical} for duplicate artist spellings under folder."""
    import config as _cfg
    from collections import Counter
    import artist_aliases
    counts = Counter()
    for f in sorted(Path(folder).rglob("*.mp3")):
        if _is_under_delete_dir(f):
            continue
        a = (_read_tags(f).get("artist") or "").strip()
        if a:
            counts[a] += 1
    groups = artist_aliases.build(counts)
    out = Path(getattr(_cfg, "ARTIST_ALIASES_FILE", BASE_DIR / "artist_aliases.json"))
    if out.exists():
        out = out.with_suffix(".proposed.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    artist_aliases.save(out, artist_aliases.to_map(groups))
    for g in groups[:20]:
        print(f"  {g['files']:5d}  {g['canonical']}  <=  {', '.join(v for v in g['variants'] if v != g['canonical'])}")
    print(f"{len(groups)} groups; map written to {out} (edit it, then --merge-artists)")


def merge_artists_folder(folder: Path, apply: bool = False):
    """Retag artist to the canonical name and move files to Artist/Album under ORGANIZED_DIR.

    Dry run by default (prints + logs/merge_artists_<ts>.csv). Never overwrites: a
    same-content file at the destination is a dupe (left in place, reported); a
    different file gets a _<n> suffix.
    """
    import config as _cfg
    import csv as _csv
    import artist_aliases
    aliases = artist_aliases.load(getattr(_cfg, "ARTIST_ALIASES_FILE", BASE_DIR / "artist_aliases.json"))
    if not aliases:
        print("no artist alias map; run --build-aliases first")
        return
    rows = []
    for f in sorted(Path(folder).rglob("*.mp3")):
        if _is_under_delete_dir(f):
            continue
        tags = _read_tags(f)
        old = (tags.get("artist") or "").strip()
        canon = aliases.get(old)
        if not canon or canon == old:
            continue
        album = (tags.get("album") or "").strip() or (tags.get("title") or f.stem)
        dest = ORGANIZED_DIR / _safe_name(canon) / _safe_name(album) / f.name
        action = "move"
        if dest.exists():
            if _file_hash(dest) == _file_hash(f):
                action = "dupe-skip"
            else:
                n = 1
                while dest.exists():
                    dest = dest.with_name(f"{f.stem}_{n}{f.suffix}")
                    n += 1
        rows.append({"src": str(f), "old_artist": old, "artist": canon, "dest": str(dest), "action": action})
        if apply:
            _set_artist(f, canon)
            if action == "move":
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"merge_artists_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with log.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=["src", "old_artist", "artist", "dest", "action"])
        w.writeheader()
        w.writerows(rows)
    if apply:
        _remove_empty_dirs(Path(folder))
    print(f"merge-artists: {len(rows)} files {'changed' if apply else 'would change (dry run, add --apply)'}; log {log}")


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
    elif _looks_like_uploader(artist, path):
        reasons.append("artist_is_uploader")

    # Title still carries lyric-video / YouTube junk
    if title and _TITLE_JUNK_RE.search(title):
        reasons.append("title_junk")

    # Artist tag swallowed "Artist - Title" (title then holds a suffix like "伴奏版")
    if artist and (re.search(r"\s[-–]\s", artist) or re.search(r"[【】#|｜]", artist)):
        reasons.append("artist_junk")
    # Title is a TV show / contest name, not a song
    if title and _SHOW_RE.search(title):
        reasons.append("title_is_show")

    from match_guard import fold
    stem_f = fold(path.stem)
    # Title that never appears in the filename: usually a different song or a talk clip
    if title and stem_f and fold(title) and fold(re.sub(r"\s*[(（][^)）]*[)）]\s*$", "", title)) not in stem_f:
        reasons.append("title_not_in_filename")
    if title and artist and fold(title) == fold(artist):
        reasons.append("artist_equals_title")
    # Cover uploads tagged with the ORIGINAL singer (COVER_ARTIST = "performer")
    import config as _cfg0
    if artist and getattr(_cfg0, "COVER_ARTIST", "performer") == "performer":
        m = re.search(r"(cover|原唱|翻自)\s*[:：]?\s*(.+)$", unicodedata.normalize("NFKC", path.stem), re.I)
        if m and fold(artist) and fold(artist) in fold(m.group(2)) and "artist_mismatch" not in reasons:
            reasons.append("cover_tagged_as_original")

    # High-precision rules disagree with the tags (e.g. a cover tagged as the original singer)
    try:
        import title_rules
        import config as _cfg
        r = title_rules.parse(path.stem) if getattr(_cfg, "USE_TITLE_RULES", True) else None
    except Exception:
        r = None
    if r:
        from match_guard import title_ok, artist_ok
        if title and not title_ok(r["title"], title) and "title_mismatch" not in reasons:
            reasons.append("title_mismatch")
        if artist and not artist_ok(r["artist"], artist) and "artist_mismatch" not in reasons:
            reasons.append("artist_mismatch")

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



def process_folder(folder: Path, fix_art: bool = True, organize: bool = True, delete: bool | None = None,
                   lyrics: bool = True, fill_only: bool | None = None, reprocess: bool = False):
    """One-pass for a mixed library.

    1. Strip YouTube junk tags
    2. Collect files whose tags are missing or look wrong (well-tagged files are left alone)
    3. LLM-retag those files (no filename regex)
    4. Lyrics: plain lyrics into the tag + synced .lrc sidecar (LRCLIB), unless lyrics=False
    5. Embed cover art (fix_art_folder), unless fix_art=False
    6. Move well-tagged files into ORGANIZED_DIR (organize_library), unless organize=False
    Skips the organized/ folder.

    fill_only (default: auto, True when folder is inside ORGANIZED_DIR): re-run
    mode for an already-organized library, see fill_missing_folder.
    reprocess=True: full re-run on every file (organized too), overwriting
    tags, lyrics and cover art; then organize as usual.
    """
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return
    if reprocess:
        print(f"reprocess: full re-tag of every file under {folder} (overwrites tags, lyrics, art)")
        reprocess_folder(folder, only_missing=False)
        if lyrics:
            print("\n--- lyrics (overwrite) ---")
            from lyrics import lyrics_folder
            lyrics_folder(folder, files=_list_mp3s(folder, include_organized=True), overwrite=True)
        if fix_art:
            print("\n--- fix art (overwrite) ---")
            fix_art_folder(folder, include_organized=True, force=True)
        if organize and not _is_under_organized(folder):
            print("\n--- organize ---")
            organize_library(folder)
        return
    if fill_only is None:
        fill_only = _is_under_organized(folder)
    if fill_only:
        fill_missing_folder(folder, fix_art=fix_art, lyrics=lyrics)
        return

    _retag_folder(folder, delete=delete)

    if lyrics:
        print("\n--- step 4: lyrics ---")
        from lyrics import lyrics_folder
        lyrics_folder(folder, files=_list_mp3s(folder))
    if fix_art:
        print("\n--- step 5: fix art ---")
        fix_art_folder(folder)
    if organize:
        print("\n--- step 6: organize ---")
        organize_library(folder)


def _fill_empty_tags(path: Path, meta: dict) -> list[str]:
    """Write album / album artist / year / genre only where the tag is empty."""
    from mutagen.mp3 import MP3
    from mutagen.id3 import TALB, TPE2, TDRC, TCON
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    filled = []
    for key, cls, val in (("TALB", TALB, meta.get("album")), ("TPE2", TPE2, meta.get("album_artist")),
                          ("TDRC", TDRC, meta.get("album_year")), ("TCON", TCON, meta.get("genre"))):
        cur = t.get(key)
        if val and not (cur and str(cur.text[0]).strip() if cur and cur.text else False):
            t[key] = cls(encoding=3, text=str(val))
            filled.append(key)
    if filled:
        audio.save()
    return filled


def fill_missing_folder(folder: Path, fix_art: bool = True, lyrics: bool = True):
    """Re-run on an already-organized folder: only fill what is empty.

    Never overwrites existing tags or art, never moves, renames or deletes
    files, and does not call the LLM. Files need title + artist already.
    1. album / album artist / year / genre from MusicBrainz -> Last.fm (empty tags only)
    2. lyrics (tag + .lrc) where missing
    3. cover art only for files with no embedded art
    """
    folder = folder.resolve()
    print(f"fill-missing: {folder} (no moves, no overwrites)")
    files = _list_mp3s(folder, include_organized=True)
    print("\n--- fill empty tags ---")
    n = 0
    for f in files:
        tags = _read_tags(f)
        title, artist = tags["title"].strip(), tags["artist"].strip()
        if not title or not artist or _has_all_basic(f):
            continue
        meta = {"title": title, "artist": artist, "album": tags["album"] or None, "file": f.name}
        try:
            meta = enrich(meta)
        except Exception as e:
            print(f"  enrich failed for {f.name}: {e}")
            continue
        meta.setdefault("album_artist", None)
        if _is_invalid_album(meta.get("album")):
            meta["album"] = None
        try:
            got = _fill_empty_tags(f, meta)
        except Exception as e:
            print(f"  tag write failed for {f.name}: {e}")
            continue
        if got:
            n += 1
            print(f"  filled {','.join(got)}: {f.name}")
    print(f"filled tags in {n} files")
    if lyrics:
        print("\n--- lyrics (missing only) ---")
        from lyrics import lyrics_folder
        lyrics_folder(folder, files=files)
    if fix_art:
        print("\n--- cover art (files without art only) ---")
        fix_art_folder(folder, include_organized=True, keep_existing=True)


def _has_all_basic(path: Path) -> bool:
    """True when album, year and genre tags are all present."""
    try:
        from mutagen.mp3 import MP3
        t = MP3(path).tags or {}
        return all(t.get(k) and t[k].text and str(t[k].text[0]).strip() for k in ("TALB", "TDRC", "TCON"))
    except Exception:
        return True


def _retag_folder(folder: Path, delete: bool | None = None):
    """Steps 1-3 of process_folder (tag cleanup + LLM on missing/wrong).

    delete: move unwanted files (non-music high confidence, 伴奏, DJ) to DELETE_DIR.
    None = config DELETE_ENABLED. False = only write the candidates list.
    """
    global _DELETE_RUN
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return
    import config as _cfg
    from deletion import DeleteRun
    enabled = getattr(_cfg, "DELETE_ENABLED", True) if delete is None else delete
    _DELETE_RUN = DeleteRun(folder, Path(getattr(_cfg, "DELETE_DIR", BASE_DIR / "_to_delete")), LOG_DIR, enabled)
    try:
        _retag_folder_inner(folder)
    finally:
        _DELETE_RUN.write_logs()
        _DELETE_RUN = None


def _version_candidates(files: list[Path]) -> list[Path]:
    """Send 伴奏/karaoke and DJ versions to the delete run; return the files that remain."""
    import config as _cfg
    from deletion import version_reason
    if _DELETE_RUN is None or not getattr(_cfg, "DELETE_VERSIONS", True):
        return files
    keep = []
    for f in files:
        why = version_reason(f.stem, _read_tags(f).get("title") or "")
        if why and _DELETE_RUN.consider(f, why, "high"):
            continue
        keep.append(f)
    return keep


def _retag_folder_inner(folder: Path):
    print(f"process: {folder} (skipping organized/)")

    print("\n--- step 1: clean junk ---")
    clean_junk_folder(folder)

    # Filename regex (from_filename_folder) is no longer run here: it wrote
    # confident wrong tags that then skipped the LLM. See tests/eval_regex.py.
    print("\n--- step 2: collect missing + wrong (伴奏/DJ versions -> delete dir) ---")
    all_mp3s = _version_candidates(_list_mp3s(folder))
    need = []
    for f in all_mp3s:
        if _is_marked_non_music(f):
            continue
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

    print("\n--- step 3: LLM on remaining ---")
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
        meta["folder_hint"] = f.parent.name
        print(f"  need ({', '.join(reasons)}): {f.name}")
        if _apply_rules(meta):
            _retag_one(meta)
            results.append(meta)
            continue
        llm_buf.append(meta)
        if len(llm_buf) >= LLM_BATCH_SIZE:
            flush()

    flush()
    finalize_allowlist(results)
    print(f"\ndone: retagged {len(results)} files")


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


DEFAULT_FOLDER = Path.home() / "Music"  # C:\\Users\\<name>\\Music on Windows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", metavar="DIR",
                    help="run the full process (metadata, lyrics, art, dedup, organize) on DIR "
                         "(default: your Music folder, Path.home()/'Music')")
    ap.add_argument("--reprocess", action="store_true",
                    help="full re-run: re-tag every file and overwrite existing tags, lyrics and art")
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
                    help=f"(regex, unreliable; not used by --process) set artist/title from 'Artist - Title' filenames (default: {MUSIC_DIR})")
    ap.add_argument("--needs-tag", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"Phase 3: list files that still need tagging (default: {MUSIC_DIR})")
    ap.add_argument("--fix-art", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"embed good cover art (keep existing, local image, or fetch) (default: {MUSIC_DIR})")
    ap.add_argument("--organize", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help=f"move well-tagged files to {ORGANIZED_DIR}/Artist/Album/ (dedupe, clean empty dirs)")
    ap.add_argument("--fix-wrong", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help="retag files whose tags disagree with the filename or look noisy")
    ap.add_argument("--process", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help="one-pass for mixed folders: clean junk, LLM on missing+wrong tags, then fix-art and organize")
    ap.add_argument("--no-fix-art", action="store_true", help="with --process: skip the fix-art step")
    ap.add_argument("--no-organize", action="store_true", help="with --process: skip the organize step")
    ap.add_argument("--no-lyrics", action="store_true", help="with --process / DIR: skip the lyrics step")
    ap.add_argument("--fill-only", action="store_true",
                    help="only fill empty tags/lyrics/art; no moves or overwrites (auto inside ORGANIZED_DIR)")
    ap.add_argument("--no-delete", action="store_true",
                    help="with --process: do not move non-music/伴奏/DJ files to DELETE_DIR (still writes the candidates list)")
    ap.add_argument("--build-aliases", nargs="?", const=str(MUSIC_DIR), type=str, metavar="DIR",
                    help="propose a duplicate-artist map from tags under DIR and write ARTIST_ALIASES_FILE (if absent; else *.proposed.json)")
    ap.add_argument("--merge-artists", nargs="?", const=str(ORGANIZED_DIR), type=str, metavar="DIR",
                    help="apply ARTIST_ALIASES_FILE: retag artist and move files to the canonical folder (dry run unless --apply)")
    ap.add_argument("--apply", action="store_true", help="with --merge-artists: actually retag and move")
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
    if args.build_aliases:
        build_aliases_folder(Path(args.build_aliases))
        return
    if args.merge_artists:
        merge_artists_folder(Path(args.merge_artists), apply=args.apply)
        return
    if args.process:
        process_folder(Path(args.process), fix_art=not args.no_fix_art, organize=not args.no_organize,
                       delete=False if args.no_delete else None,
                       lyrics=not args.no_lyrics, fill_only=True if args.fill_only else None,
                       reprocess=args.reprocess)
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

    # default: one-pass process on DIR (or MUSIC_DIR)
    process_folder(Path(args.folder).expanduser() if args.folder else DEFAULT_FOLDER,
                   fix_art=not args.no_fix_art, organize=not args.no_organize,
                   delete=False if args.no_delete else None,
                       lyrics=not args.no_lyrics, fill_only=True if args.fill_only else None,
                       reprocess=args.reprocess)

if __name__ == "__main__":
    main()
