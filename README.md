# yt-music-pipeline

Automated YouTube music library builder. Searches, downloads, tags, dedupes,
and organizes singles into a clean local library with proper metadata.

## Pipeline

```
YouTube search --> yt-dlp download (MP3 320k + cover)
      --> regex parse (title/artist/featured)
      --> allowlist + local SQLite cross-check (MusicBrainz cache)
      --> suspicious items --> local Qwen 3.5 9B (batch LLM correction)
      --> MusicBrainz/iTunes/Deezer enrichment (canonical artist, album, year)
      --> rapidfuzz dedup (threshold 85)
      --> 1:1 cropped cover + border
      --> Processed/Artist/Album/Title.mp3
```

## Files

| File | Role |
|------|------|
| `config.py` | All parameters: paths, search queries, artist seeds, LLM endpoint, API keys, dedup threshold |
| `search_download.py` | yt-dlp keyword + artist searches, duration filters, download archive |
| `regex_parser.py` | Regex title parsing, multi-artist split (feat/ft/with/by/翻唱/cover), allowlist + DB cross-check, suspicious flagging |
| `parse_meta.py` | Orchestrates regex -> LLM batches -> allowlist updates |
| `local_db.py` | SQLite cache: artists, albums, tracks from MusicBrainz |
| `mb_cache_builder.py` | One-time/weekly populate of local_db from MB API |
| `musicbrainz.py` | Live MB lookup + iTunes/Deezer fallback, canonical artist names |
| `dedup.py` | rapidfuzz similarity dedup |
| `artwork.py` | 1:1 crop + border, JPG |
| `artist_seed.py` | Region helpers for artist lists |
| `main.py` | Full orchestrator with --dry-run / --skip-dl |

## Artist seeds

~250 artists across 8 regions (cn/kr/jp/eu/us/ca/au/la), weighted by popularity.
Chinese artists get the highest weight. Edit `ARTIST_QUERIES` in config.py.

## API keys (optional, put in config.py)

- `LASTFM_API_KEY` - Last.fm (free at last.fm/api)
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` - Spotify for Developers (free)
- `DISCOGS_TOKEN` - Discogs (free token)

MusicBrainz, iTunes, Deezer, Cover Art Archive need no key.

## Local LLM

Points at `http://192.168.86.35:11434/v1` (Qwen 3.5 9B).
Only suspicious/failed regex parses go to the LLM, in batches of 20.

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

## Install & run

```bash
pip install -r requirements.txt
python mb_cache_builder.py    # populate local DB (slow, ~1 req/sec)
python main.py --dry-run      # test search + parse
python main.py                # full run
```

Cron example in `cron.example` (weekly Sunday 2 AM).

## Notes

- Original YouTube filenames are preserved in staging/ for traceability
- Processed files get a .json sidecar with raw title + parsed fields
- Album falls back to song title when no album is found (never guesses wrong)
- Cover art: 600x600 with 8px border, dark background
