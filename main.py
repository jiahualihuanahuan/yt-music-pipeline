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
from dedup import dedup
from artwork import process_cover
from local_db import init_db, connect, stats


def organize(results):
    """Move/copy staged files into Processed/Artist/Album/Title.mp3 + cover."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    organized = []
    for meta in results:
        src = STAGING_DIR / meta["file"]
        if not src.exists():
            continue
        artist = meta.get("artist_canonical") or meta.get("artist") or "Unknown"
        album = meta.get("album") or meta.get("title") or "Unknown"
        title = meta.get("title") or src.stem
        # sanitize folder names
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
        # write a small sidecar json for traceability
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
        organized.append(meta)
    return organized


def _watch_and_parse(stop_event: threading.Event, result_q: Queue):
    """Watch staging for new mp3s. Parse with regex immediately.
    Send LLM batches as soon as LLM_BATCH_SIZE is reached.
    """
    seen = set()
    llm_buf = []
    all_results = []

    while not stop_event.is_set() or any(STAGING_DIR.glob("*.mp3")):
        new_files = []
        for f in STAGING_DIR.glob("*.mp3"):
            if f.name not in seen:
                # skip files still being written (size not stable)
                try:
                    size1 = f.stat().st_size
                    time.sleep(0.3)
                    size2 = f.stat().st_size
                    if size1 == size2 and size1 > 0:
                        new_files.append(f)
                except OSError:
                    continue

        for f in new_files:
            seen.add(f.name)
            meta = parse_one(f)
            all_results.append(meta)
            print(f"parsed: {f.name}  needs_llm={meta.get('needs_llm')}")
            if meta.get("needs_llm"):
                llm_buf.append(meta)
                if len(llm_buf) >= LLM_BATCH_SIZE:
                    apply_llm_batch(llm_buf)
                    llm_buf = []

        # brief pause before next poll
        if not stop_event.is_set():
            time.sleep(2)

    # final flush of remaining LLM items
    if llm_buf:
        apply_llm_batch(llm_buf)

    finalize_allowlist(all_results)
    result_q.put(all_results)


def run_parallel():
    """Download in background while parsing new files as they arrive."""
    stop_event = threading.Event()
    result_q: Queue = Queue()

    # start watcher
    watcher = threading.Thread(target=_watch_and_parse, args=(stop_event, result_q), daemon=True)
    watcher.start()

    # run downloads (blocks until all searches done)
    try:
        run_downloads()
    finally:
        stop_event.set()

    watcher.join(timeout=600)
    try:
        results = result_q.get(timeout=5)
    except Empty:
        print("warning: watcher returned no results")
        results = []
    return results


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
        # sequential path for dry-run / process-only
        results = parse_all()
    else:
        results = run_parallel()

    print(f"parsed {len(results)} files")

    enriched = []
    for meta in results:
        try:
            enriched.append(enrich(meta))
        except Exception as e:
            print(f"enrich failed for {meta.get('file')}: {e}")
            enriched.append(meta)

    kept = dedup(enriched)
    organized = organize(kept)

    # summary
    summary = {
        "run_at": datetime.now().isoformat(),
        "total_parsed": len(results),
        "after_dedup": len(kept),
        "organized": len(organized),
        "high_confidence": sum(1 for m in organized if m.get("confidence") == "high"),
        "llm_used": sum(1 for m in organized if m.get("llm_notes") is not None),
    }
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    (LOG_DIR / f"run_{datetime.now():%Y%m%d_%H%M%S}.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
