#!/usr/bin/env python3
"""Music pipeline orchestrator.

Usage:
  python main.py              # full run (download + post-process in parallel)
  python main.py --dry-run    # search + parse only, no downloads
  python main.py --skip-dl    # skip download phase, process existing staging
"""
import argparse
import json
import shutil
import threading
import time
from pathlib import Path
from datetime import datetime
from queue import Queue, Empty

from config import (STAGING_DIR, PROCESSED_DIR, LOG_DIR, DRY_RUN,
                    ALLOWLIST_PATH, LLM_BATCH_SIZE)
from search_download import main as run_downloads
from parse_meta import parse_one, apply_llm_batch, finalize_allowlist, parse_all
from musicbrainz import enrich
from dedup import dedup, is_duplicate
from artwork import process_cover
from local_db import init_db, connect, stats


def organize_one(meta):
    """Move one staged file into Processed/Artist/Album/Title.mp3 + cover."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    src = STAGING_DIR / meta["file"]
    if not src.exists():
        return None
    artist = meta.get("artist_canonical") or meta.get("artist") or "Unknown"
    album = meta.get("album") or meta.get("title") or "Unknown"
    title = meta.get("title") or src.stem
    safe = lambda s: "".join(c for c in s if c not in '/\\:*?"<>|').strip() or "Unknown"
    dest_dir = PROCESSED_DIR / safe(artist) / safe(album)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{safe(title)}.mp3"
    if src.resolve() != dest.resolve():
        shutil.move(str(src), str(dest))
    # cover art
    thumb = src.with_suffix(".jpg")
    if not thumb.exists():
        thumb = Path(str(src) + ".jpg")
    cover_dst = dest_dir / "cover.jpg"
    if thumb.exists():
        process_cover(thumb, cover_dst)
    meta["processed_path"] = str(dest)
    # sidecar
    sidecar = dest.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "raw_youtube_title": meta.get("raw"),
        "title": meta.get("title"),
        "artist": meta.get("artist"),
        "featured": meta.get("featured"),
        "artist_canonical": meta.get("artist_canonical"),
        "album": meta.get("album"),
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

    with lock:
        if is_duplicate(meta, kept):
            print(f"dedup skip: {meta.get('file')}")
            # remove the staged file so it doesn't get reprocessed
            src = STAGING_DIR / meta["file"]
            if src.exists():
                src.unlink(missing_ok=True)
            return
        kept.append(meta)

    result = organize_one(meta)
    if result:
        with lock:
            organized.append(result)
        print(f"organized: {meta.get('artist_canonical') or meta.get('artist')} - {meta.get('title')}")


def _watch_and_process(stop_event: threading.Event, organized: list, lock: threading.Lock):
    """Watch staging. Parse immediately. LLM in batches.
    As soon as a file is ready (no LLM needed, or LLM done), enrich + dedup + organize.
    """
    seen = set()
    llm_buf = []
    all_parsed = []
    kept = []  # for incremental dedup

    def flush_llm():
        nonlocal llm_buf
        if not llm_buf:
            return
        apply_llm_batch(llm_buf)
        for m in llm_buf:
            _process_ready(m, kept, organized, lock)
        llm_buf = []

    while not stop_event.is_set() or any(STAGING_DIR.glob("*.mp3")):
        new_files = []
        for f in STAGING_DIR.glob("*.mp3"):
            if f.name not in seen:
                try:
                    size1 = f.stat().st_size
                    time.sleep(0.2)
                    size2 = f.stat().st_size
                    if size1 == size2 and size1 > 0:
                        new_files.append(f)
                except OSError:
                    continue

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
                # no LLM needed — process immediately
                _process_ready(meta, kept, organized, lock)

        if not stop_event.is_set():
            time.sleep(1.5)

    # final LLM flush
    flush_llm()
    finalize_allowlist(all_parsed)


def run_parallel():
    """Download in background while parsing / enriching / organizing new files."""
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
        run_downloads()
    finally:
        stop_event.set()

    worker.join(timeout=900)
    return organized


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-dl", action="store_true")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    init_db()
    print("local DB stats:", stats())

    if args.dry_run:
        import config as cfg
        cfg.DRY_RUN = True

    if args.skip_dl or args.dry_run:
        # sequential path
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
        organized = run_parallel()

    # summary
    summary = {
        "run_at": datetime.now().isoformat(),
        "organized": len(organized),
        "high_confidence": sum(1 for m in organized if m.get("confidence") == "high"),
        "llm_used": sum(1 for m in organized if m.get("llm_notes") is not None),
    }
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    (LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
