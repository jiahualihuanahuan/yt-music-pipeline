"""Search YouTube and download singles via yt-dlp.

Uses keyword queries (SEARCH_QUERIES) and artist-name queries (ARTIST_QUERIES).
Filters: duration range, playlist/compilation exclusion, download archive.

Deno is required by yt-dlp for YouTube. This module finds it permanently
(even after reinstalls) and passes the correct flags.
"""
import os
import random
import shutil
import subprocess
import time
from pathlib import Path
from config import (SEARCH_QUERIES, ARTIST_QUERIES, STAGING_DIR, ARCHIVE_PATH,
                    AUDIO_FORMAT, AUDIO_QUALITY, MIN_DURATION, MAX_DURATION,
                    MAX_PLAYLIST_ITEMS, SEARCH_RESULTS, SEARCH_SLEEP, DRY_RUN)

STAGING_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_PATH.touch(exist_ok=True)

# Duration filter via match-filters (yt-dlp has no --min-duration / --max-duration)
DURATION_FILTER = f"duration >= {MIN_DURATION} & duration <= {MAX_DURATION}"


def _find_deno() -> str | None:
    """Locate deno permanently. Checks PATH then the standard install location."""
    # 1. already on PATH
    found = shutil.which("deno")
    if found:
        return found
    # 2. default install location used by the official installer
    default = Path.home() / ".deno" / "bin" / "deno"
    if default.is_file():
        return str(default)
    # 3. common system locations
    for p in ("/usr/local/bin/deno", "/usr/bin/deno"):
        if Path(p).is_file():
            return p
    return None


def _ensure_deno_path():
    """Add ~/.deno/bin to PATH for this process and child processes."""
    deno_bin = Path.home() / ".deno" / "bin"
    if deno_bin.is_dir():
        path = os.environ.get("PATH", "")
        if str(deno_bin) not in path.split(os.pathsep):
            os.environ["PATH"] = f"{deno_bin}{os.pathsep}{path}"


_ensure_deno_path()
DENO_PATH = _find_deno()

if DENO_PATH:
    print(f"Deno found: {DENO_PATH}")
else:
    print("WARNING: Deno not found. Install with: curl -fsSL https://deno.land/install.sh | sh")
    print("         Then re-run. YouTube downloads will 403 without it.")

COMMON_FLAGS = [
    "--extract-audio", f"--audio-format={AUDIO_FORMAT}",
    f"--audio-quality={AUDIO_QUALITY}",
    "--embed-thumbnail", "--add-metadata",
    f"--download-archive={ARCHIVE_PATH}",
    "--match-filters", DURATION_FILTER,
    f"--max-downloads={MAX_PLAYLIST_ITEMS}",
    "--no-playlist", "--ignore-errors", "--no-overwrites",
    # Prefer clients and formats that currently 403 less often
    "--extractor-args", "youtube:player_client=android_sdkless,web,mweb",
    "-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
    # Reduce 403s / YouTube blocks
    "--sleep-requests", "1",
    "--sleep-interval", "2",
    "--retries", "5",
    "--fragment-retries", "5",
    "--remote-components", "ejs:github",
    "-o", str(STAGING_DIR / "%(title)s.%(ext)s"),
]

if DENO_PATH:
    COMMON_FLAGS.extend(["--js-runtimes", f"deno:{DENO_PATH}"])

ARTWORK_FLAGS = ["--convert-thumbnails", "jpg"]


def _run(query):
    cmd = ["yt-dlp", f"ytsearch{SEARCH_RESULTS}:{query}", *COMMON_FLAGS, *ARTWORK_FLAGS]
    print("SEARCH:", query)
    if DRY_RUN:
        print("  (dry-run)")
        return
    # ensure PATH includes deno for any subprocesses yt-dlp spawns
    env = os.environ.copy()
    subprocess.run(cmd, check=False, env=env)
    time.sleep(SEARCH_SLEEP + random.uniform(0, 1))


def run_keyword_searches():
    # weighted shuffle: higher weight = more likely early
    pool = []
    for q, w in SEARCH_QUERIES:
        pool.extend([q] * w)
    random.shuffle(pool)
    # dedupe while preserving order
    seen = set()
    for q in pool:
        if q not in seen:
            seen.add(q)
            _run(q)


def run_artist_searches():
    # rotate by weight; append music terms so we get songs, not interviews/vlogs
    pool = []
    for name, region, w in ARTIST_QUERIES:
        pool.extend([name] * w)
    random.shuffle(pool)
    seen = set()
    for name in pool:
        if name not in seen:
            seen.add(name)
            _run(f"{name} official audio OR lyrics OR 歌曲")


def main():
    run_keyword_searches()
    run_artist_searches()
    print("download phase done")


if __name__ == "__main__":
    main()
