# yt-music-pipeline

Automated YouTube music library builder. Searches, downloads, tags, dedupes,
and organizes singles into a clean local library with proper metadata.

## Pipeline

```
YouTube search --> yt-dlp download (MP3 320k + cover)
      --> regex parse (title/artist/featured)          └── parallel
      --> allowlist + local SQLite cross-check          └── as files arrive
      --> suspicious items --> local Qwen (batch LLM)   └── while downloading
      --> MusicBrainz/iTunes/Deezer enrichment
      --> rapidfuzz dedup (threshold 85)
      --> 1:1 cropped cover + border
      --> Processed/Artist/Album/Title.mp3
```

Downloads and post-processing run in parallel. As soon as a file finishes
downloading it is parsed, optionally sent to the LLM, enriched, deduped,
and organized — no need to wait for the whole download phase to finish.

## Files

| File | Role |
|------|------|
| `config.py` | All parameters: paths, search queries, artist seeds, LLM endpoint, API keys, dedup threshold |
| `config_local.py` | Optional local overrides (gitignored). Redefine any variable from config.py here. |
| `search_download.py` | yt-dlp keyword + artist searches, duration filters, download archive, Deno/cookies support |
| `regex_parser.py` | Regex title parsing, multi-artist split (feat/ft/with/by/翻唱/cover), allowlist + DB cross-check, suspicious flagging |
| `parse_meta.py` | Orchestrates regex -> LLM batches -> allowlist updates. Supports incremental use. |
| `local_db.py` | SQLite cache: artists, albums, tracks from MusicBrainz |
| `mb_cache_builder.py` | One-time/weekly populate of local_db from MB API |
| `musicbrainz.py` | Live MB lookup + iTunes/Deezer fallback, canonical artist names |
| `dedup.py` | rapidfuzz similarity dedup (also incremental) |
| `artwork.py` | 1:1 crop + border, JPG |
| `artist_seed.py` | Region helpers for artist lists |
| `main.py` | Full orchestrator: parallel by default, `--dry-run` / `--skip-dl` |
| `setup_deno.sh` | One-time Deno install + PATH setup (required by yt-dlp for YouTube) |

## Quick start

```bash
pip install -r requirements.txt
bash setup_deno.sh            # install Deno (needed to avoid 403s)
# optional: export YouTube cookies to cookies.txt and place in project root
python mb_cache_builder.py    # populate local DB (slow, ~1 req/sec)
python main.py --dry-run      # test search + parse
python main.py                # full parallel run
```

## Resume after interrupt

If downloads finished (or partially finished) but post-processing was killed:

```bash
python main.py --skip-dl
```

This skips the download phase and processes whatever is still in `staging/`.
Files already moved to `processed/` are left alone.

## Local config overrides

Do **not** edit `config.py` directly if you want painless `git pull`s.
Create `config_local.py` (gitignored) and override any setting:

```python
# config_local.py
MAX_PLAYLIST_ITEMS = 50
LLM_BATCH_SIZE = 3
LLM_BASE_URL = "http://192.168.86.35:8001/v1"
LLM_API_KEY = "vllm-secret-key"
LLM_MODEL = "Qwen/Qwen3-8B-AWQ"
```

## YouTube 403 / bot checks

YouTube requires a JavaScript runtime and often cookies.

1. Run `bash setup_deno.sh` once (installs Deno and adds it to PATH).
2. Export cookies from your browser (extension: **Get cookies.txt LOCALLY**)
   while logged into YouTube, save as `cookies.txt`, and put it in the project root.
3. The pipeline auto-detects both and passes the correct yt-dlp flags.

Cookies expire; re-export when you start seeing "Sign in to confirm you're not a bot".

## Parallel processing

By default `python main.py` runs downloads and post-processing at the same time:

- A background watcher scans `staging/` for finished MP3s.
- Files that don't need the LLM are enriched / deduped / organized immediately.
- Files that need the LLM are batched (default 5) and processed as soon as a batch is full.
- Dedup is incremental against already-organized tracks.
- At the end any remaining LLM items are flushed.

## Artist seeds

~250 artists across 8 regions (cn/kr/jp/eu/us/ca/au/la), weighted by popularity.
Chinese artists get the highest weight. Edit `ARTIST_QUERIES` in config.py
or override in `config_local.py`.

## API keys (optional)

Put in `config.py` or `config_local.py`:

- `LASTFM_API_KEY` - Last.fm (free at last.fm/api)
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` - Spotify for Developers (free)
- `DISCOGS_TOKEN` - Discogs (free token)

MusicBrainz, iTunes, Deezer, Cover Art Archive need no key.

## Local LLM

Points at `http://192.168.86.35:8001/v1` (vLLM serving Qwen/Qwen3-8B-AWQ) by default.
Requires the API key `vllm-secret-key` (Bearer token).
Only suspicious/failed regex parses go to the LLM.

Current defaults (tune in `config_local.py`):

- `LLM_MODEL = "Qwen/Qwen3-8B-AWQ"`
- `LLM_BATCH_SIZE = 15`
- `LLM_TIMEOUT = None` (no timeout)
- `LLM_RETRIES = 2`

If the LLM is unreachable or times out, the regex result is kept and processing continues.

## Cross-check logic

1. Regex parses title/artist
2. Artist in allowlist -> pass
3. Artist in local SQLite (MB-verified) -> pass (strong signal)
4. Title in local DB -> weak hint only (covers share titles with originals)
5. Neither -> flag suspicious -> LLM
6. Multi-artist markers (feat/ft/with/by/翻唱/cover/原唱/&/,) -> split, all names to allowlist

## Dedup

Compares normalized "title + artist" via rapidfuzz token_sort_ratio.
Score >= 85 = duplicate; keeps the higher-confidence copy.
Works both in batch mode and incrementally during a parallel run.

## Cron

See `cron.example` for a weekly Sunday 2 AM example.

## Notes

- Original YouTube filenames are preserved in staging/ for traceability
- Processed files get a .json sidecar with raw title + parsed fields
- Album falls back to song title when no album is found (never guesses wrong)
- Cover art: 600x600 with 8px border, dark background
- `cookies.txt`, `config_local.py`, `staging/`, `processed/`, `logs/`, and the download archive are gitignored
