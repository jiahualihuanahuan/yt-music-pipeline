#!/usr/bin/env python3
"""Music pipeline orchestrator.

Usage:
  python main.py                              # full run (download + post-process in parallel)
  python main.py --dry-run                    # search + parse only, no downloads
  python main.py --skip-dl                    # skip download phase, process existing staging
  python main.py --reprocess /path/to/music   # retag all MP3s in folder (in place)
  python main.py --fill-missing /path/to/music # only retag files missing title/artist/album
"""
import argparse
import json
import shutil
import threading
import time
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
from dedup import dedup, is_duplicate
from artwork import process_cover
from local_db import init_db, stats
from search_download import main as run_downloads, download_url

CHANNEL_NOISE = {
    "official", "channel", "topic", "vevo", "music", "records", "entertainment",
    "official audio", "official video", "official mv", "lyrics", "hd", "4k",
}


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
):
    """Write ID3v2.4 tags including embedded cover art (APIC) and release year (TDRC)."""
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import TIT2, TPE1, TALB, TPE2, TDRC, TXXX, APIC
        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        aa = album_artist or artist or "Unknown"
        tags["TIT2"] = TIT2(encoding=3, text=title or "Unknown")
        tags["TPE1"] = TPE1(encoding=3, text=artist or "Unknown")
        tags["TALB"] = TALB(encoding=3, text=album or title or "Unknown")
        tags["TPE2"] = TPE2(encoding=3, text=aa)
        if year:
            tags["TDRC"] = TDRC(encoding=3, text=str(year))
        if featured:
            tags["TXXX:FEATURED"] = TXXX(encoding=3, desc="FEATURED", text=", ".join(featured))
        if cover_path and cover_path.exists():
            tags.delall("APIC")
            tags["APIC"] = APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # Front cover
                desc="Cover",
                data=cover_path.read_bytes(),
            )
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

    _write_tags(
        dest,
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        featured=meta.get("featured"),
        year=meta.get("album_year"),
        cover_path=cover_dst if cover_dst.exists() else None,
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

    if not meta.get("album"):
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
            all_parsed.append(meta)
            print(f"parsed: {f.name}  needs_llm={meta.get('needs_llm')}")
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


def reprocess_folder(folder: Path, only_missing: bool = False):
    """Re-parse and retag MP3s under folder, in place.
    If only_missing=True, skip files that already have title + artist + album.
    Never renames files.
    """
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"not a directory: {folder}")
        return

    all_mp3s = sorted(folder.rglob("*.mp3"))
    if only_missing:
        mp3s = []
        for f in all_mp3s:
            tags = _read_tags(f)
            if not (tags["title"] and tags["artist"] and tags["album"]):
                mp3s.append(f)
        print(f"fill-missing: {len(mp3s)} / {len(all_mp3s)} files need tags")
    else:
        mp3s = all_mp3s
        print(f"reprocess: {len(mp3s)} files under {folder}")

    llm_buf = []
    results = []

    def flush():
        nonlocal llm_buf
        if not llm_buf:
            return
        apply_llm_batch(llm_buf)
        for m in llm_buf:
            if not m.get("artist") and m.get("folder_artist"):
                m["artist"] = m["folder_artist"]
            _retag_one(m)
            results.append(m)
        llm_buf = []

    for f in mp3s:
        meta = parse_one(f)
        meta["path"] = str(f)
        fb = _folder_fallback(f)
        meta["folder_artist"] = fb["folder_artist"]
        meta["folder_album"] = fb["folder_album"]
        if not meta.get("album") and fb["folder_album"]:
            meta["album"] = fb["folder_album"]

        print(f"parsed: {f.name}  artist={meta.get('artist')}  needs_llm={meta.get('needs_llm')}")
        if meta.get("needs_llm"):
            llm_buf.append(meta)
            if len(llm_buf) >= LLM_BATCH_SIZE:
                flush()
        else:
            if not meta.get("artist") and fb["folder_artist"]:
                meta["artist"] = fb["folder_artist"]
            _retag_one(meta)
            results.append(meta)

    flush()
    finalize_allowlist(results)
    print(f"done: {len(results)} files")


def _retag_one(meta):
    """Enrich and write tags in place. Never renames the file."""
    path = Path(meta.get("path") or "")
    if not path.exists():
        return
    try:
        meta = enrich(meta)
    except Exception as e:
        print(f"enrich failed for {path.name}: {e}")

    title = meta.get("title") or path.stem
    artist = _clean_artist(
        meta.get("artist_canonical")
        or meta.get("artist")
        or meta.get("folder_artist")
        or "Unknown"
    )
    album = meta.get("album") or meta.get("folder_album") or title
    album_artist = meta.get("album_artist") or artist

    if not artist or artist.lower() in ("", "none", "null"):
        artist = meta.get("folder_artist") or "Unknown"
    if not album:
        album = title or "Unknown"

    cover_path = path.parent / "cover.jpg"
    _write_tags(
        path,
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        featured=meta.get("featured"),
        year=meta.get("album_year"),
        cover_path=cover_path if cover_path.exists() else None,
    )
    print(f"tagged: {artist} - {title}  [{album}]")

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-dl", action="store_true")
    ap.add_argument("--reprocess", type=str, metavar="DIR",
                    help="reparse + retag all MP3s under DIR in place")
    ap.add_argument("--fill-missing", type=str, metavar="DIR",
                    help="only retag MP3s missing title, artist, or album")
    ap.add_argument("--url", "--playlist", dest="url", type=str, metavar="URL",
                help="download and process only a specific YouTube playlist or video URL")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    init_db()
    print("local DB stats:", stats())

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
    (LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()