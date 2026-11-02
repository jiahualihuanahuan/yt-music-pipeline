# yt-music-pipeline

Builds and maintains a local MP3 library from YouTube. It has two jobs:

1. **Download mode** (`python main.py`): searches YouTube with keyword and artist queries (or one URL), downloads MP3s with `yt-dlp`, works out title, artist and album with regex plus an optional local LLM, enriches and dedupes them, and files them under `processed/Artist/Album/`.
2. **Library mode** (`python main.py --process DIR`, `--fix-art`, `--organize`, …): cleans up the tags of an existing MP3 folder in place, and can move well-tagged files into `ORGANIZED_DIR`.

There is no test suite. Everything lives in flat Python modules and is set up in `config.py`.

## Requirements

- Python 3.10+ (the code uses `X | None` type hints)
- Python packages from `requirements.txt`: `yt-dlp`, `requests`, `rapidfuzz`, `Pillow`, `musicbrainzngs`, `openai`, `mutagen`
  (`musicbrainzngs` and `openai` are listed but never imported. The code calls the HTTP APIs directly with `requests`.)
- **ffmpeg** on `PATH`, which `yt-dlp --extract-audio` needs for MP3 conversion and thumbnails (download mode only)
- **Deno**, used by yt-dlp as its JavaScript runtime for YouTube. Install it with `bash setup_deno.sh`
- Optional: an OpenAI-compatible LLM endpoint (for example vLLM) and a SearXNG instance (see [LLM](#llm))

```bash
pip install -r requirements.txt
bash setup_deno.sh                 # installs Deno, adds ~/.deno/bin to PATH, symlinks to /usr/local/bin if writable
python mb_cache_builder.py         # optional: pre-fill the local MusicBrainz cache (~1 req/s)
python main.py --dry-run           # print search queries without downloading
```

## Configuration

All settings live in `config.py`. To change them, create **`config_local.py`** (gitignored) and redefine any variable. It is imported at the end of `config.py`, and the paths derived from `BASE_DIR` are recomputed afterwards.

```python
# config_local.py
from pathlib import Path
BASE_DIR = Path("/data/yt-music")                 # working data root
MUSIC_DIR = Path("/srv/music")                     # default DIR for library-mode flags
ORGANIZED_DIR = Path("/srv/music-organized")       # target of --organize / --process
LLM_BASE_URL = "http://localhost:8001/v1"
LLM_MODEL = "Qwen/Qwen3-8B-AWQ"
BROWSER_COOKIES = "firefox"                        # or None to disable the fallback
```

The code reads no environment variables. Everything is set in Python config.

### Paths

| Setting | Default | Used for |
|---|---|---|
| `BASE_DIR` | `/Volumes/AI/music` if it exists and is writable, else `~/yt-music-pipeline` (created), else `<repo>/data` | root for the paths below |
| `STAGING_DIR` | `BASE_DIR/staging` | raw yt-dlp downloads (original YouTube filenames) |
| `PROCESSED_DIR` | `BASE_DIR/processed` | download-mode output: `Artist/Album/<original name>.mp3` |
| `ARCHIVE_PATH` | `BASE_DIR/downloaded.txt` | yt-dlp download archive (no re-downloads) |
| `ALLOWLIST_PATH` | `BASE_DIR/artist_allowlist.json` | learned known artists |
| `LOCAL_DB_PATH` | `BASE_DIR/musicbrainz_cache.db` | SQLite artist/album/track cache |
| `LOG_DIR` | `BASE_DIR/logs` | run summaries, `--dump-tags` / `--needs-tag` reports |
| `COOKIES_PATH` | `BASE_DIR/cookies.txt` | optional YouTube cookies (**in `BASE_DIR`, not the repo root**) |
| `MUSIC_DIR` | `/mnt/data/docker/navidrome/music` | default `DIR` when a library flag is given without one |
| `ORGANIZED_DIR` | `/mnt/data/docker/navidrome/organized` | well-tagged library. Library scans skip it |

Note: `MUSIC_DIR` and `ORGANIZED_DIR` are hard-coded Linux/Navidrome paths and are **not** derived from `BASE_DIR`. Override them on other machines.

### Download settings

`AUDIO_FORMAT="mp3"`, `AUDIO_QUALITY="320"`, `MIN_DURATION=90` / `MAX_DURATION=600` seconds (live streams are excluded too), `MAX_PLAYLIST_ITEMS=1` (max downloads per search), `SEARCH_RESULTS=1` (`ytsearchN`), `SEARCH_SLEEP=1`. `SEARCH_QUERIES` holds weighted keyword queries and `ARTIST_QUERIES` holds about 300 `(artist, region, weight)` seeds across cn/kr/jp/eu/us/ca/au/la. `BROWSER_COOKIES` is not in `config.py`. It defaults to `"chrome"` and is only used when `cookies.txt` is missing.

### Other settings

`DEDUP_THRESHOLD=85` (rapidfuzz), the `LLM_*` and `SEARXNG_*` settings (below), `USER_AGENT`, `MB_CONTACT`, and `DRY_RUN`.

`LASTFM_API_KEY`, `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` and `DISCOGS_TOKEN` are defined but **not used anywhere** in the code.

## LLM

Filenames that regex can't parse confidently (and files picked out by the library modes) go to an OpenAI-compatible `POST {LLM_BASE_URL}/chat/completions` in batches.

| Setting | Default |
|---|---|
| `LLM_BASE_URL` | `http://192.168.86.35:8001/v1` |
| `LLM_API_KEY` | `vllm-secret-key` (sent as a Bearer token if set) |
| `LLM_MODEL` | `Qwen/Qwen3-8B-AWQ` |
| `LLM_TIMEOUT` | `None` (no timeout) |
| `LLM_BATCH_SIZE` | `15` |
| `LLM_RETRIES` | `2` |

Items the LLM is still unsure about get a second pass with web-search context from **SearXNG** (`SEARXNG_BASE_URL`, default `http://192.168.86.35:8099`, up to 3 results each). SearXNG must have the `json` format turned on. If the LLM fails, the regex result is kept (download mode), or the file is left unwritten (library modes).

## Download mode

```bash
python main.py                       # keyword + artist searches, parallel post-processing
python main.py --url <URL>           # only this video/playlist/channel (alias: --playlist)
python main.py --dry-run             # print the queries that would run, then parse whatever is already in staging/
python main.py --skip-dl             # no download; post-process what is already in staging/
```

What a full run does:

1. **Search & download** (`search_download.py`): runs each weighted keyword query, then each artist query (`"<name> 歌曲 official"` for cn and `"<name> official audio"` otherwise), then up to 15 random artists from the allowlist. yt-dlp extracts MP3 320 audio plus a JPG thumbnail into `staging/`, using the download archive, the duration filter, Deno, and `cookies.txt` (or `--cookies-from-browser`).
2. **Parallel watcher** (`main.py`): while downloads run, a thread picks up finished MP3s in `staging/`:
   - regex parse (`regex_parser.py`): splits feat/ft/with/cover markers and checks against the allowlist and the local SQLite DB
   - album lookup first when title and artist are known
   - files flagged `needs_llm` are batched (`LLM_BATCH_SIZE`) to the LLM
   - enrichment (`musicbrainz.py`: MusicBrainz, then iTunes/Deezer fallback, canonical artist names)
   - incremental fuzzy dedup (`dedup.py`, title+artist `token_sort_ratio >= 85`, keeps the higher-confidence item)
   - organize: move to `processed/<Artist>/<Album>/<original filename>.mp3`, write a 1:1 600×600 `cover.jpg` with an 8px dark border, fetch lyrics from LRCLIB, write ID3v2.4 tags (cover, lyrics, year), and add a `.json` sidecar with the raw title and parsed fields
   - afterwards it updates the allowlist and deletes orphaned staging sidecars
3. A summary (`organized`, `high_confidence`, `llm_used`) is printed and saved to `logs/run_<timestamp>.json`.

Album falls back to the title when no valid album is found. Filenames are never renamed.

`--dry-run` and `--skip-dl` ignore `--url`. Neither runs the parallel watcher. They parse, enrich, dedupe and organize whatever is already in `staging/`. **`--dry-run` still moves and tags files in `staging/`. It only skips the yt-dlp calls.**

## Library mode

Each flag takes an optional `DIR` (default `MUSIC_DIR`) and works recursively on `*.mp3` files, skipping hidden files. Only **one** command runs per call. If several are given, the first one in this order wins: `--dump-tags`, `--clean-junk`, `--from-filename`, `--needs-tag`, `--fix-art`, `--organize`, `--fix-wrong`, `--process`, `--llm-only`, `--remove-tags`, `--lyrics`, `--fill-missing`, `--reprocess`.

Commands marked "skips organized" ignore files under `ORGANIZED_DIR`.

| Command | What it does |
|---|---|
| `--process [DIR]` | One-pass cleanup, see below. Recommended. |
| `--clean-junk [DIR]` | Strip YouTube junk `TXXX` frames and useless genres. Keeps cover, title, artist, album, lyrics, MusicBrainz. Skips organized. |
| `--from-filename [DIR]` | Set artist/title from `Artist - Title.mp3` filenames (album falls back to title). Skips organized. |
| `--needs-tag [DIR]` | Report files missing a valid title/artist/album to `logs/needs_tag_<ts>.txt`. Skips organized. |
| `--fix-wrong [DIR]` | LLM-retag files whose tags disagree with the filename, or have a bad album or channel-like artist. Skips organized. |
| `--fix-art [DIR]` | Make sure each MP3 has good embedded art: keep an existing APIC (≥8 KB and ≥200×200), else use a nearby `cover.jpg`/`folder.jpg`/`<name>.jpg`/largest `*.jpg`, else fetch from iTunes, then Deezer (needs title+artist). Art is cropped to 600×600 with a border. Skips organized. |
| `--organize [DIR]` | Move well-tagged files (valid title, artist, album) to `ORGANIZED_DIR/<Artist>/<Album>/<filename>`. **Deletes** exact duplicates (same size + MD5) of files already organized. Adds `_<size>` to the name if one exists. Removes empty directories in `DIR` and `ORGANIZED_DIR`. Badly tagged files are left in place. |
| `--fill-missing [DIR]` | Retag (regex/enrich/LLM) only files missing a valid title/artist/album. Files with a MusicBrainz ID and valid artist/title are skipped. |
| `--reprocess [DIR]` | Re-parse and retag every MP3 in place. |
| `--llm-only [DIR]` | Send every filename to the LLM (no regex) and overwrite tags in place. |
| `--remove-tags [DIR]` | Strip text ID3 tags but **keep embedded cover art**. |
| `--lyrics [DIR]` | Add LRCLIB lyrics (USLT) to files that already have title+artist. |
| `--dump-tags [DIR]` | Write every file's ID3 frames to `logs/dump_tags_<ts>.txt`. |

`--fill-missing`, `--reprocess`, `--llm-only`, `--remove-tags`, `--lyrics` and `--dump-tags` use their own `rglob` and **do not** skip `ORGANIZED_DIR`.

### `--process` step by step

```bash
python main.py --process /srv/music
python main.py --process /srv/music --no-fix-art --no-organize   # retag only
```

1. **clean junk**: same as `--clean-junk`
2. **from filename**: same as `--from-filename`
3. **collect**: find files that are not well tagged, or whose tags look wrong (same checks as `--fix-wrong`)
4. **LLM**: retag those files in batches of `LLM_BATCH_SIZE` and update the allowlist (skipped if nothing needs it)
5. **fix art**: same as `--fix-art` on `DIR` (skip with `--no-fix-art`)
6. **organize**: same as `--organize` on `DIR`, moving files into `ORGANIZED_DIR` (skip with `--no-organize`)

`--no-fix-art` and `--no-organize` have no effect without `--process`.

## Other scripts

- `python mb_cache_builder.py`: for each artist in `ARTIST_QUERIES`, fetches up to 100 releases from MusicBrainz into `musicbrainz_cache.db` (1.1 s between artists)
- `python search_download.py`: download phase only (no post-processing)
- `python artist_seed.py`: print the top 10 seeds per region
- `cron.example`: weekly run every Sunday at 02:00 (`main.py >> logs/cron.log`). Fix the path, and note that `logs/` is relative to the repo, not `BASE_DIR`

## Folder layout

```
BASE_DIR/
  staging/                     raw downloads + yt-dlp .jpg/.info.json (cleaned up after organize)
  processed/Artist/Album/      download-mode output: *.mp3, *.json sidecar, cover.jpg
  logs/                        run_*.json, dump_tags_*.txt, needs_tag_*.txt
  downloaded.txt               yt-dlp archive
  artist_allowlist.json
  musicbrainz_cache.db
  cookies.txt                  optional
ORGANIZED_DIR/Artist/Album/    library-mode output from --organize / --process
```

## Troubleshooting

- **HTTP 403 / "Sign in to confirm you're not a bot"**: run `bash setup_deno.sh` and put a fresh `cookies.txt` (Netscape format, for example from the "Get cookies.txt LOCALLY" extension) in `BASE_DIR`. You get a warning when it's older than 7 days. Without it, yt-dlp falls back to `--cookies-from-browser chrome`. Set `BROWSER_COOKIES` to change or disable this.
- **"Deno not found" / cookie warnings on every command**: these are printed when `search_download.py` is imported, even in library modes. They are harmless there.
- **`could not create download dirs`**: `BASE_DIR` isn't writable. Override it in `config_local.py`. Library modes still work.
- **LLM errors / hangs**: check `LLM_BASE_URL` and `LLM_MODEL`. `LLM_TIMEOUT=None` means a stuck server blocks forever, so set a number.
- **Nothing gets organized**: only files with a valid title, artist **and** album are moved. Run `--needs-tag` to see why.
- **No such directory**: library flags default to `MUSIC_DIR` (`/mnt/data/docker/navidrome/music`). Pass `DIR` or override it.

## Known gaps

- `musicbrainz.py` defines `MB_API`, `ITUNES_API` and `DEEZER_API` as Markdown link strings (`"[https://…](https://…)"`), so live MusicBrainz/iTunes/Deezer enrichment requests in that module fail. Cover fetching (`artwork.py`) and `mb_cache_builder.py` use correct URLs.
- `config.py` ships real-looking Last.fm/Spotify keys that aren't used. Consider removing them.
- There are no tests.
