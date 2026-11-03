#!/usr/bin/env python3
"""Standalone tagging app: tag, dedupe and organize MP3s already on disk.

Wraps main.process_folder. No downloading, no Spotify. Works on Windows,
macOS and Linux (pure Python + pathlib).

    python tag_music.py "D:\\Music"          (Windows cmd / Anaconda Prompt)
    python tag_music.py ~/Music              (macOS / Linux)
    tag-music ~/Music                        (after `pip install -e .`)
"""
import argparse
import sys
from pathlib import Path

DESCRIPTION = """Tag a folder of MP3 files that are already on your computer.

Steps: clean YouTube junk tags -> work out title/artist (title rules + local LLM)
-> fill album/year/genre/cover from MusicBrainz (Cover Art Archive), Last.fm as
fallback -> lyrics from LRCLIB (tag + .lrc) -> embed 600x600 cover art (Vroom
player) -> remove exact duplicates and move well-tagged files into
ORGANIZED_DIR/Artist/Album/.

Re-running on a folder inside ORGANIZED_DIR (or with --fill-only) only fills what
is empty: no overwrites, moves, renames or deletes.

Nothing is downloaded. Settings come from config.py, overridden by config_local.py
(LLM_BASE_URL / LLM_MODEL for the local LLM, optional LASTFM_API_KEY,
MUSIC_DIR, ORGANIZED_DIR, DELETE_DIR)."""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tag-music", description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", metavar="FOLDER",
                    help="folder with MP3 files (searched recursively). Default: MUSIC_DIR from config")
    ap.add_argument("--no-fix-art", action="store_true", help="skip the cover-art step")
    ap.add_argument("--no-organize", action="store_true",
                    help="tag in place only; do not move files into ORGANIZED_DIR (no dedup)")
    ap.add_argument("--no-lyrics", action="store_true", help="skip the lyrics step (LRCLIB)")
    ap.add_argument("--fill-only", action="store_true",
                    help="re-run mode: only fill empty tags, lyrics and missing art; never overwrite, "
                         "move, rename or delete. Automatic when FOLDER is inside ORGANIZED_DIR")
    ap.add_argument("--no-delete", action="store_true",
                    help="do not move non-music / 伴奏 / DJ versions to DELETE_DIR (only list them)")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    import main as pipeline  # imported late so --help works without config/deps
    folder = Path(args.folder).expanduser() if args.folder else Path(pipeline.MUSIC_DIR)
    if not folder.is_dir():
        print(f"not a folder: {folder}", file=sys.stderr)
        return 2
    try:
        pipeline.init_db()
    except Exception as e:
        print(f"WARNING: could not init local DB: {e}")
    pipeline.process_folder(folder, fix_art=not args.no_fix_art, organize=not args.no_organize,
                            delete=False if args.no_delete else None,
                            lyrics=not args.no_lyrics, fill_only=True if args.fill_only else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
