#!/usr/bin/env python3
"""Music pipeline orchestrator.

Usage:
  python main.py              # full run
  python main.py --dry-run    # search + parse only, no downloads
  python main.py --skip-dl    # skip download phase
"""
import argparse
import json
import shutil
from pathlib import Path
from datetime import datetime

from config import (STAGING_DIR, PROCESSED_DIR, LOG_DIR, DRY_RUN,
                    ALLOWLIST_PATH)
from search_download import main as run_downloads
from parse_meta import parse_all
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-dl", action="store_true")
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    init_db()
    print("local DB stats:", stats())

    if not args.skip_dl:
        if args.dry_run:
            import config as cfg
            cfg.DRY_RUN = True
        run_downloads()

    if args.dry_run:
        import config as cfg
        cfg.DRY_RUN = True

    results = parse_all()
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
