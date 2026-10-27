"""Search YouTube and download singles via yt-dlp.

Uses keyword queries (SEARCH_QUERIES), artist-name queries (ARTIST_QUERIES),
and discovered allowlist artists (ALLOWLIST_PATH).
Filters: duration range, live stream exclusion, download archive.

Deno is required by yt-dlp for YouTube. This module finds it permanently
(even after reinstalls) and passes the correct flags.
"""
import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path
import config as cfg
from config import (
    SEARCH_QUERIES, ARTIST_QUERIES, ALLOWLIST_PATH, STAGING_DIR, ARCHIVE_PATH,
    AUDIO_FORMAT, AUDIO_QUALITY, MIN_DURATION, MAX_DURATION,
    MAX_PLAYLIST_ITEMS, SEARCH_RESULTS, SEARCH_SLEEP,
    COOKIES_PATH,
)

def _ensure_dirs():
    """Create staging and archive only when actually downloading."""
    try:
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ARCHIVE_PATH.touch(exist_ok=True)
    except OSError as e:
        print(f"WARNING: could not create download dirs ({STAGING_DIR}): {e}")
        print("         This is fine for --fill-missing / --reprocess. Override BASE_DIR in config_local.py for downloads.")

# Duration + non-live filter via match-filters
DURATION_FILTER = f"duration >= {MIN_DURATION} & duration <= {MAX_DURATION} & !is_live & !was_live"


def _find_deno() -> str | None:
    """Locate deno permanently. Checks PATH then the standard install location."""
    found = shutil.which("deno")
    if found:
        return found
    default = Path.home() / ".deno" / "bin" / "deno"
    if default.is_file():
        return str(default)
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
    # Write standalone .jpg to staging so artwork.py can crop 1:1 + add border
    "--write-thumbnail", "--convert-thumbnails", "jpg",
    "--add-metadata",
    f"--download-archive={ARCHIVE_PATH}",
    "--match-filters", DURATION_FILTER,
    f"--max-downloads={MAX_PLAYLIST_ITEMS}",
    "--no-playlist", "--ignore-errors", "--no-overwrites",
    "--windows-filenames",
    # Prioritize pure audio streams (m4a/opus) over format 18
    "-f", "bestaudio[ext=m4a]/bestaudio/best",
    # Reduce 403s / YouTube blocks
    "--sleep-requests", "1",
    "--sleep-interval", "2",
    "--retries", "5",
    "--fragment-retries", "5",
    "--remote-components", "ejs:github",
    # Prefer clients that work better with cookies
    "--extractor-args", "youtube:player_client=web,web_safari,android",
    "-o", str(STAGING_DIR / "%(title)s.%(ext)s"),
]

if DENO_PATH:
    COMMON_FLAGS.extend(["--js-runtimes", f"deno:{DENO_PATH}"])

# ---------- Cookies (cookies.txt takes priority) ----------
BROWSER_COOKIES = getattr(cfg, "BROWSER_COOKIES", "chrome")

if COOKIES_PATH.exists():
    # Check file age as a rough freshness hint
    age_days = (time.time() - COOKIES_PATH.stat().st_mtime) / 86400
    COMMON_FLAGS.extend(["--cookies", str(COOKIES_PATH)])
    print(f"Using cookies file: {COOKIES_PATH} (age: {age_days:.1f} days)")
    if age_days > 7:
        print("WARNING: cookies.txt is older than 7 days. YouTube may reject it.")
        print("         Re-export from a private window and replace the file.")
else:
    print(f"WARNING: cookies.txt not found at {COOKIES_PATH}")
    print("         YouTube will likely block downloads with 'Sign in to confirm you\u2019re not a bot'.")
    print("         Export cookies from a private window (see README) and place it there.")
    if BROWSER_COOKIES:
        COMMON_FLAGS.extend(["--cookies-from-browser", BROWSER_COOKIES])
        print(f"Falling back to --cookies-from-browser {BROWSER_COOKIES}")
    else:
        print("No browser cookies configured either. Downloads may fail.")


def _run(query: str):
    _ensure_dirs()
    cmd = ["yt-dlp", f"ytsearch{SEARCH_RESULTS}:{query}", *COMMON_FLAGS]
    print("SEARCH:", query)
    if cfg.DRY_RUN:
        print("  (dry-run)")
        return
    env = os.environ.copy()
    subprocess.run(cmd, check=False, env=env)
    time.sleep(SEARCH_SLEEP + random.uniform(0, 1))


def run_keyword_searches():
    pool = []
    for q, w in SEARCH_QUERIES:
        pool.extend([q] * w)
    random.shuffle(pool)
    seen = set()
    for q in pool:
        if q not in seen:
            seen.add(q)
            _run(q)


def run_artist_searches():
    pool = []
    for name, region, w in ARTIST_QUERIES:
        pool.extend([(name, region)] * w)
    random.shuffle(pool)
    seen = set()
    for name, region in pool:
        if name not in seen:
            seen.add(name)
            suffix = "歌曲 official" if region == "cn" else "official audio"
            _run(f"{name} {suffix}")

    # Also sample up to 15 newly discovered artists from allowlist
    if ALLOWLIST_PATH.exists():
        try:
            allow = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
            new_artists = [a for a in allow if a not in seen]
            for name in random.sample(new_artists, min(15, len(new_artists))):
                _run(f"{name} official audio")
        except Exception:
            pass
def download_url(url: str, max_items: int | None = None):
    """Download a single playlist, channel, or video URL directly into STAGING_DIR."""
    _ensure_dirs()
    # Remove --no-playlist and --max-downloads from COMMON_FLAGS so the full playlist downloads
    flags = [
        f for f in COMMON_FLAGS
        if f != "--no-playlist" and not f.startswith("--max-downloads=")
    ]
    flags.append("--yes-playlist")
    if max_items:
        flags.append(f"--max-downloads={max_items}")

    cmd = ["yt-dlp", url, *flags]
    print("DOWNLOAD URL:", url)
    if cfg.DRY_RUN:
        print("  (dry-run)")
        return
    env = os.environ.copy()
    subprocess.run(cmd, check=False, env=env)

def main():
    _ensure_dirs()
    run_keyword_searches()
    run_artist_searches()
    print("download phase done")


if __name__ == "__main__":
    main()
