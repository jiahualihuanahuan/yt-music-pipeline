# yt-music-pipeline

Cleans up, tags, dedupes and organizes MP3s that are **already on disk** (e.g. a library for the Vroom player). It does not search or download anything from YouTube.

```bash
python main.py /path/to/music        # the whole process on one folder (default: MUSIC_DIR)
```

That one command runs: junk-tag cleanup -> metadata (title rules / local LLM + MusicBrainz, Last.fm fallback) -> artwork -> dedup + organize into `ORGANIZED_DIR`. See [`--process` step by step](#--process-step-by-step). Single steps are available as flags (see [Library mode](#library-mode)).

Tests: `python -m unittest discover -s tests -t .` (mocked HTTP, no network). Everything lives in flat Python modules and is set up in `config.py`.

## Requirements

- Python 3.10+ (the code uses `X | None` type hints)
- Python packages from `requirements.txt`: `requests`, `rapidfuzz`, `Pillow`, `musicbrainzngs`, `openai`, `mutagen`
  (`musicbrainzngs` and `openai` are listed but never imported. The code calls the HTTP APIs directly with `requests`.)
- Optional: a Last.fm API key (see [Metadata sources](#metadata-sources))
- Optional: an OpenAI-compatible LLM endpoint (for example vLLM) and a SearXNG instance (see [LLM](#llm))

```bash
pip install -r requirements.txt
python mb_cache_builder.py         # optional: pre-fill the local MusicBrainz cache (~1 req/s)
python main.py /path/to/music      # run the process on existing MP3s
```

## Configuration

All settings live in `config.py`. To change them, create **`config_local.py`** (gitignored) and redefine any variable. It is imported at the end of `config.py`, and the paths derived from `BASE_DIR` are recomputed afterwards.

```python
# config_local.py  (start from: cp config_local.example.py config_local.py)
from pathlib import Path
BASE_DIR = Path("/data/yt-music")                 # working data root
MUSIC_DIR = Path("/srv/music")                     # default DIR for library-mode flags
ORGANIZED_DIR = Path("/srv/music-organized")       # target of --organize / --process
LLM_BASE_URL = "http://localhost:8001/v1"
LLM_MODEL = "Qwen/Qwen3-8B-AWQ"
LASTFM_API_KEY = "..."
BROWSER_COOKIES = "firefox"                        # or None to disable the fallback
```

The code reads no environment variables. Everything is set in Python config.

### Paths

| Setting | Default | Used for |
|---|---|---|
| `BASE_DIR` | `/Volumes/AI/music` if it exists and is writable, else `~/yt-music-pipeline` (created), else `<repo>/data` | root for the paths below |
| `ALLOWLIST_PATH` | `BASE_DIR/artist_allowlist.json` | learned known artists |
| `LOCAL_DB_PATH` | `BASE_DIR/musicbrainz_cache.db` | SQLite artist/album/track cache |
| `LOG_DIR` | `BASE_DIR/logs` | run summaries, `--dump-tags` / `--needs-tag` reports |
| `MUSIC_DIR` | `/mnt/data/docker/navidrome/music` | default `DIR` for `python main.py` and library flags given without one |
| `ORGANIZED_DIR` | `/mnt/data/docker/navidrome/organized` | well-tagged library. Library scans skip it |

Note: `MUSIC_DIR` and `ORGANIZED_DIR` are hard-coded Linux/Navidrome paths and are **not** derived from `BASE_DIR`. Override them on other machines.

### Other settings

`USE_REGEX_PARSER=False` (see [Parsing](#parsing)), `DEDUP_THRESHOLD=85` (rapidfuzz), the `LLM_*` and `SEARXNG_*` settings (below), `USER_AGENT` and `MB_CONTACT`. `ARTIST_QUERIES` (~300 artist seeds) feeds `mb_cache_builder.py` and artist casing. Leftover download settings in `config.py` (`SEARCH_*`, `AUDIO_*`, `STAGING_DIR`, …) are unused.

`LASTFM_API_KEY` defaults to an empty string and should be set in `config_local.py`. Spotify is no longer used; old `SPOTIFY_*` lines in `config_local.py` are ignored. `DISCOGS_TOKEN` is unused.

## Metadata sources

`musicbrainz.enrich()` fills canonical artist, album, year, genre and a cover URL. Later sources only fill fields that are still empty:

1. **Local MusicBrainz cache** (SQLite, filled by `mb_cache_builder.py` and live lookups)
2. **Live MusicBrainz** API (artist search + releases, 1.1 s between calls): canonical artist, album, year and the release id. No key needed. Set `MB_CONTACT`.
3. **Cover Art Archive** (`coverartarchive.org/release/<mbid>/front-500`): cover for the matched MusicBrainz release, no key
4. **Last.fm** `track.getInfo` (≤4 req/s), fallback: album (if MB had none), album image (if no CAA cover), and top tags as the genre (junk tags like "seen live" are dropped)
5. **iTunes**, then **Deezer**, only if the album is still missing

**Match guard** (`match_guard.py`): a Last.fm, iTunes or Deezer result is only used when both its title and artist are ≥85 similar (rapidfuzz) to what was searched, ignoring case, brackets like `(From "Moana")`, fullwidth forms and Traditional/Simplified Chinese. This stops "Falling You" from becoming Céline Dion's "Falling into You". Items flagged non-music are never enriched.

A source is skipped silently when its key is empty. On an HTTP or network error it logs one line and moves on. Last.fm and Cover Art Archive results are cached in memory per (title, artist) for the run.

Genre is written to the ID3 `TCON` tag. `--fix-art` fetches covers in this order: MusicBrainz/Cover Art Archive, iTunes, Deezer, Last.fm. Spotify is not used.

## Parsing

Regex title parsing is **off by default** (`USE_REGEX_PARSER = False`): every file's name goes to the LLM. `--process` no longer runs the filename-regex step. `--from-filename` still exists as a standalone command but isn't recommended.

On 56 realistic YouTube titles (`python tests/eval_regex.py`):

| Parser | correct | wrong | abstain |
|---|---|---|---|
| `--from-filename` (`Artist - Title` split) | 7 | 17 | 32 |
| `parse_title` (download mode) | 25 | 22 | 9 |

All 17 wrong `--from-filename` results looked well-tagged afterwards, so `--process` never sent them to the LLM.

### Regex maintenance principle

Accuracy first, coverage second. The regex is a first pass that only answers when a filename matches one of its known patterns. A pattern is kept only if it is right almost every time it answers (currently 99.1% accurate on the files it handles, covering about 19% of the library). Anything it can't confidently match goes to the local LLM, with a prompt that includes worked examples from this library. When adding or changing patterns, prefer fewer, stricter patterns over broader ones that risk mislabeling.

### High-precision title rules (`title_rules.py`, on by default)

Instead of the general regex, a small set of rules handles only a few unambiguous shapes and abstains on everything else (`USE_TITLE_RULES = True`):

- lyric-channel uploads: `Artist - Title『lyric line』【動態歌詞…】` (`⧸` splits featured artists)
- covers: `【Performer】…【Title】…Cover：Original` / `【Performer】…Title-Cover：Original`
- `Artist《Title》Official MV`
- `Artist - Title (Official Video|Audio)`

On a 203-row sample of a real library they accepted 55 rows (27%) at 98.2% precision (54/55; the miss kept an uploader's typo). On the 56 synthetic titles they accepted 12, all correct. Everything else goes to the LLM.

`clean_raw()` also normalizes fullwidth punctuation (NFKC), `⧸` → `/`, and strips a trailing `[videoId]` before anything reaches the LLM; `--organize` drops `[videoId]` from file names.

### What `--process` sends to the LLM

Besides missing tags, a file is re-tagged when: the artist looks like an uploader (`…VEVO`, `… - Topic`, `Disney…`, `Holy007`-style handles), the title still has `『』【】`/Official/HD junk, the title tag never appears in the filename (Traditional/Simplified-insensitive), artist equals title, the title rules disagree with the tags, or (with `COVER_ARTIST = "performer"`) a cover is tagged with the original singer.

### Non-music

The LLM returns `is_music` and `confidence`. Non-music items (talk clips, vlogs, news, podcasts, cartoon episodes) get no guessed tags. Children's songs and TV-show stage/Live performances count as music; livestream clips (直播歌切 etc.) do not.

With `NON_MUSIC_ACTION = "delete"` (default) non-music answered with confidence `high` is moved to the delete folder (below); anything less confident is only listed as `review` and marked `TXXX:PIPELINE_NON_MUSIC` so `--process`/`--organize` skip it. `"skip"` only marks, `"move"` moves to `NON_MUSIC_DIR`.

### Delete folder (nothing is ever removed)

"Delete" means **move to `DELETE_DIR`** (default `BASE_DIR/_to_delete`), keeping the path relative to the scanned folder and never overwriting (a `_<n>` suffix is added). It runs as part of `--process` for:

- non-music with LLM confidence `high`
- 伴奏 / karaoke-version / off-vocal backing tracks (`deletion.version_reason`; quoted lyrics and "with vocals" markers such as うた入り are ignored)
- livestream song clips (`直播歌切`, `直播唱`, `【歌切】`, `歌回`, live stream), reason `livestream`; the LLM also marks them non-music
- DJ versions (`DJ版`, `DJ某某版`, `DJ0.9降速版`, `(DJ…)`, `DJ抖音`; not artists like "DJ Snake")

Every run writes `logs/delete_candidates_<ts>.csv` (path, reason, confidence, action = moved / review / dry-run); actual moves go to `logs/deleted_<ts>.csv` (src, dest, reason). Use `--no-delete` (or `DELETE_ENABLED = False`) to only write the candidates list; `DELETE_VERSIONS = False` keeps 伴奏/DJ files. All library commands skip `DELETE_DIR`. Undo = move files back using `deleted_<ts>.csv`.

### Conventions

- Covers: artist = the performer; the original singer goes to `TXXX:ORIGINAL_ARTIST` (`WRITE_ORIGINAL_ARTIST`).
- Original script is kept (no Traditional/Simplified conversion); bilingual titles and artist aliases in brackets (`某某(本名)`) are kept.
- Soundtrack/Disney: the real singer via MusicBrainz/Last.fm (match guard) or the LLM; the movie only when unknown.
- Playlist/channel folders are only a hint for the artist; files are organized into Artist/Album.
- Well-tagged files are organized only; their tags are not touched.

### Duplicate artist folders

```bash
python main.py --build-aliases /srv/music        # propose ARTIST_ALIASES_FILE (JSON {variant: canonical})
python main.py --merge-artists /srv/music/organized          # dry run: logs/merge_artists_<ts>.csv
python main.py --merge-artists /srv/music/organized --apply  # retag artist + move to canonical folder
```

Spellings are grouped by their Chinese name (Traditional/Simplified-insensitive); an English-only name joins only when exactly one Chinese name is seen with it in a bilingual credit. Multi-artist credits and junk ("A - Title", `【】`) are never merged. Canonical = the bilingual form if one exists, Simplified Chinese spelling preferred, then the most used; with no Simplified spelling the name is converted to Simplified (artist names only, titles keep their script). If the file exists, `--build-aliases` writes `*.proposed.json` instead, so your edits are kept. `--merge-artists` never overwrites: same content at the destination is reported as `dupe-skip`, a different file gets a `_<n>` suffix. `--organize` also applies the map.

### Evaluating the prompt on your library

```bash
python main.py --dump-tags ...   # or any CSV with relative_path, filename, artist, title, album
python tests/eval_llm_prompt.py --csv sample.csv --dry-run                 # show what would be sent
python tests/eval_llm_prompt.py --csv sample.csv --labels labels.json      # real LLM calls, prints precision
```

Writes `logs/llm_eval_<time>.csv`; never touches music files.

## LLM

Filenames the title rules don't accept (and files picked out by the library modes) go to an OpenAI-compatible `POST {LLM_BASE_URL}/chat/completions` in batches.

| Setting | Default |
|---|---|
| `LLM_BASE_URL` | `http://192.168.86.35:8001/v1` |
| `LLM_API_KEY` | `vllm-secret-key` (sent as a Bearer token if set) |
| `LLM_MODEL` | `Qwen/Qwen3-8B-AWQ` |
| `LLM_TIMEOUT` | `None` (no timeout) |
| `LLM_BATCH_SIZE` | `15` |
| `LLM_RETRIES` | `2` |

Items the LLM is still unsure about get a second pass with web-search context from **SearXNG** (`SEARXNG_BASE_URL`, default `http://192.168.86.35:8099`, up to 3 results each). SearXNG must have the `json` format turned on. If the LLM fails, the file is left unwritten.

## Library mode

Each flag takes an optional `DIR` (default `MUSIC_DIR`) and works recursively on `*.mp3` files, skipping hidden files. Only **one** command runs per call. If several are given, the first one in this order wins: `--dump-tags`, `--clean-junk`, `--from-filename`, `--needs-tag`, `--fix-art`, `--organize`, `--fix-wrong`, `--build-aliases`, `--merge-artists`, `--process`, `--llm-only`, `--remove-tags`, `--lyrics`, `--fill-missing`, `--reprocess`.

Commands marked "skips organized" ignore files under `ORGANIZED_DIR`.

| Command | What it does |
|---|---|
| `--process [DIR]` | One-pass cleanup, see below. Recommended. |
| `--clean-junk [DIR]` | Strip YouTube junk `TXXX` frames and useless genres. Keeps cover, title, artist, album, lyrics, MusicBrainz. Skips organized. |
| `--from-filename [DIR]` | (Regex; unreliable, see [Parsing](#parsing).) Set artist/title from `Artist - Title.mp3` filenames (album falls back to title). Skips organized. |
| `--needs-tag [DIR]` | Report files missing a valid title/artist/album to `logs/needs_tag_<ts>.txt`. Skips organized. |
| `--fix-wrong [DIR]` | LLM-retag files whose tags disagree with the filename, or have a bad album or channel-like artist. Skips organized. |
| `--fix-art [DIR]` | Make sure each MP3 has good embedded art: keep an existing APIC (≥8 KB and ≥200×200), else use a nearby `cover.jpg`/`folder.jpg`/`<name>.jpg`/largest `*.jpg`, else fetch from MusicBrainz/Cover Art Archive, iTunes, Deezer, then Last.fm (needs title+artist). Art is cropped to 600×600 with a border. Skips organized. |
| `--organize [DIR]` | Move well-tagged files (valid title, artist, album) to `ORGANIZED_DIR/<Artist>/<Album>/<filename>`. **Deletes** exact duplicates (same size + MD5) of files already organized. Adds `_<size>` to the name if one exists. Removes empty directories in `DIR` and `ORGANIZED_DIR`. Badly tagged files are left in place. |
| `--fill-missing [DIR]` | Retag (LLM + enrichment) only files missing a valid title/artist/album. Files with a MusicBrainz ID and valid artist/title are skipped. |
| `--reprocess [DIR]` | Re-parse and retag every MP3 in place. |
| `--llm-only [DIR]` | Send every filename to the LLM (no regex) and overwrite tags in place. |
| `--remove-tags [DIR]` | Strip text ID3 tags but **keep embedded cover art**. |
| `--lyrics [DIR]` | Add LRCLIB lyrics (USLT) to files that already have title+artist. |
| `--dump-tags [DIR]` | Write every file's ID3 frames to `logs/dump_tags_<ts>.txt`. |
| `--build-aliases [DIR]` | Propose a duplicate-artist map, see [Duplicate artist folders](#duplicate-artist-folders). |
| `--merge-artists [DIR] [--apply]` | Merge duplicate artist spellings (dry run unless `--apply`). Default DIR is `ORGANIZED_DIR`. |

`--fill-missing`, `--reprocess`, `--llm-only`, `--remove-tags`, `--lyrics` and `--dump-tags` use their own `rglob` and **do not** skip `ORGANIZED_DIR`.

### `--process` step by step

```bash
python main.py /srv/music                      # same as --process /srv/music
python main.py --process /srv/music
python main.py --process /srv/music --no-fix-art --no-organize   # retag only
```

1. **clean junk**: same as `--clean-junk` (also replaces channel-tab albums like "X - Videos" with the title)
2. **delete versions + collect**: move 伴奏/DJ versions to `DELETE_DIR` (unless `--no-delete`), then find files that are not well tagged, or whose tags look wrong (same checks as `--fix-wrong`)
3. **LLM**: retag those files in batches of `LLM_BATCH_SIZE`, then enrich (MusicBrainz, then Last.fm, …) and update the allowlist (skipped if nothing needs it)
4. **fix art**: same as `--fix-art` on `DIR` (skip with `--no-fix-art`)
5. **organize**: same as `--organize` on `DIR`, moving files into `ORGANIZED_DIR` (skip with `--no-organize`)

`--no-fix-art`, `--no-organize` and `--no-delete` have no effect without `--process`.

## Other scripts

- `python mb_cache_builder.py`: for each artist in `ARTIST_QUERIES`, fetches up to 100 releases from MusicBrainz into `musicbrainz_cache.db` (1.1 s between artists)
- `python artist_seed.py`: print the top 10 seeds per region

## Folder layout

```
BASE_DIR/
  logs/                        run_*.json, dump_tags_*.txt, needs_tag_*.txt
  artist_allowlist.json
  musicbrainz_cache.db
ORGANIZED_DIR/Artist/Album/    library-mode output from --organize / --process
```

## Troubleshooting

- **LLM errors / hangs**: check `LLM_BASE_URL` and `LLM_MODEL`. `LLM_TIMEOUT=None` means a stuck server blocks forever, so set a number.
- **Nothing gets organized**: only files with a valid title, artist **and** album are moved. Run `--needs-tag` to see why.
- **No such directory**: library flags default to `MUSIC_DIR` (`/mnt/data/docker/navidrome/music`). Pass `DIR` or override it.

## Known gaps

- Older commits of `config.py` contain Last.fm/Spotify keys. Rotate them (Spotify is no longer used, so its app can simply be deleted).
- Genre comes from Last.fm tags (MusicBrainz releases do not carry a genre here).
- Tests cover the metadata sources, config overrides, the regex switch, title rules, wrong-tag detection, the enrichment guard and non-music handling (synthetic fixtures), not the full pipeline.
