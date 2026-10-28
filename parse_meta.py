"""Metadata parsing orchestrator.

1. regex_parser (+ .info.json inspection) on every file
2. collect needs_llm items into batches
3. first LLM pass: title-only (no search)
4. second LLM pass: SearXNG only for items where LLM left title/artist empty
5. merge + validate results, update allowlist
"""
import json
import re
import time
import requests
from pathlib import Path
from config import (
    STAGING_DIR, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT,
    LLM_BATCH_SIZE, LLM_RETRIES, ALLOWLIST_PATH, LLM_API_KEY,
    SEARXNG_BASE_URL, SEARXNG_MAX_RESULTS,
)
from regex_parser import parse_title
from local_db import connect, lookup_artist, normalize

SYSTEM_PROMPT = """You are a music metadata expert. Given a JSON array of items, decide the correct tags.

Each item may contain:
- "id": integer you must echo back
- "raw": YouTube video title / filename (primary signal)
- "channel": optional uploader/channel hint
- "existing_title", "existing_artist", "existing_album": current ID3 tags (may be wrong)
- "web_search": optional short list of web snippets (title + content). Present ONLY when a previous pass was unsure. Use them to fill fields you would otherwise leave null.

Rules:
- Only fill a field if you are confident it is correct.
- If you are not confident, or the existing value is clearly wrong / nonsense / emoji / generic, return null for that field so it can be cleared.
- Prefer correcting or clearing bad existing tags over guessing.
- "title" and "artist" are nullable: return null if truly unknown. Do not invent.
- Chinese titles: part AFTER dash/colon is often the artist and BEFORE is the title, OR Artist《Title》. Use knowledge + channel hint.
- Hyphenated names like "A-Lin", "G-Dragon", "j-hope", "(G)I-DLE" are ONE artist.
- Multiple artists (feat/ft/with/翻唱/cover/原唱/&/、): primary in "artist", others in "featured" (array). Empty array if none.
- Strip noise: "Official Music Video", "MV", "完整版", "歌词版", "Lyrics", "4K", "HD".
- "album" is nullable: return null if unsure. The pipeline will fall back to the song title. Never return emoji, "music", "unknown", etc.
- Keep the exact "id".
- Respond with ONLY a valid JSON array, no markdown, no commentary:
  [{"id": 0, "title": "..." or null, "artist": "..." or null, "featured": [], "album": "..." or null, "notes": "..." or null}]
"""

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


def _extract_json_array(text: str) -> list:
    cleaned = _THINK_RE.sub("", text).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = _JSON_ARRAY_RE.search(cleaned)
        if m:
            return json.loads(m.group(0))
        raise


def _searxng_search(query: str, max_results: int = 3) -> list[dict]:
    """Query local SearXNG and return top results as {title, content}."""
    if not query or not SEARXNG_BASE_URL:
        return []
    url = f"{SEARXNG_BASE_URL.rstrip('/')}/search"
    try:
        r = requests.get(
            url,
            params={"q": query, "format": "json"},
            headers={"User-Agent": "yt-music-pipeline/1.0"},
            timeout=12,
        )
        if r.status_code == 403:
            print(f"SearXNG JSON disabled (403). Enable formats: [html, json] in settings.yml")
            return []
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get("results") or []:
            title = (item.get("title") or "").strip()
            content = (item.get("content") or "").strip()
            if title or content:
                results.append({
                    "title": title[:120],
                    "content": content[:180],
                })
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        print(f"SearXNG search failed for {query!r}: {e}")
        return []


def _build_search_query(item: dict) -> str:
    """Build a short search query from raw title."""
    raw = (item.get("raw") or "").strip()
    raw = re.sub(r"[\(\[].*?(lyrics|official|mv|hd|4k).*?[\)\]]", "", raw, flags=re.I)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:120]


def _call_llm(batch: list[dict], use_search: bool = False) -> list[dict] | None:
    """Call the LLM. Search is only attached when use_search=True."""
    req_items = []
    for idx, b in enumerate(batch):
        item = {"id": idx, "raw": b["raw"]}
        if b.get("channel_hint"):
            item["channel"] = b["channel_hint"]
        if b.get("existing_title"):
            item["existing_title"] = b["existing_title"]
        if b.get("existing_artist"):
            item["existing_artist"] = b["existing_artist"]
        if b.get("existing_album"):
            item["existing_album"] = b["existing_album"]

        # Only search when the caller says the LLM was unsure
        if use_search:
            query = _build_search_query(item)
            if query:
                hits = _searxng_search(query, min(SEARXNG_MAX_RESULTS, 3))
                if hits:
                    item["web_search"] = hits
                    print(f"  SearXNG (unsure): {len(hits)} hits for {query!r}")

        req_items.append(item)

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(req_items, ensure_ascii=False)},
        ],
        "temperature": 0.1,
    }
    timeout = None if not LLM_TIMEOUT else LLM_TIMEOUT
    headers = {}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    last_err = None
    for attempt in range(1, LLM_RETRIES + 1):
        try:
            r = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            if r.status_code >= 400:
                body = r.text[:500]
                print(f"LLM HTTP {r.status_code}: {body}")
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            parsed = _extract_json_array(content)
            if isinstance(parsed, list):
                return parsed
        except Exception as e:
            last_err = e
            print(f"LLM batch attempt {attempt}/{LLM_RETRIES} failed: {e}")
            if attempt < LLM_RETRIES:
                time.sleep(3)
    print(f"LLM batch failed after {LLM_RETRIES} attempts: {last_err}")
    return None


def _update_allowlist(artist_names):
    existing = set()
    if ALLOWLIST_PATH.exists():
        try:
            existing = set(json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    added = set()
    try:
        db = connect()
    except Exception as e:
        print(f"allowlist update skipped (db unavailable): {e}")
        return
    try:
        for name in artist_names:
            n = normalize(name)
            if not n or n in existing:
                continue
            if lookup_artist(db, n):
                existing.add(n)
                added.add(n)
    finally:
        db.close()
    if added:
        try:
            ALLOWLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
            ALLOWLIST_PATH.write_text(json.dumps(sorted(existing), ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"allowlist +{len(added)}: {sorted(added)[:10]}")
        except OSError as e:
            print(f"allowlist write skipped ({ALLOWLIST_PATH}): {e}")


def _load_info_json(mp3_path: Path) -> dict | None:
    info_path = mp3_path.with_suffix(".info.json")
    if info_path.exists():
        try:
            return json.loads(info_path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _load_info_and_channel(mp3_path: Path) -> tuple[dict | None, str | None]:
    """Load optional .info.json or read uploader/artist embedded by yt-dlp --add-metadata."""
    info_path = mp3_path.with_suffix(".info.json")
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            uploader = (info.get("artist") or info.get("creator") or info.get("uploader") or info.get("channel") or "").strip()
            return info, (uploader or None)
        except Exception:
            pass

    try:
        from mutagen.mp3 import MP3
        audio = MP3(mp3_path)
        if audio.tags and "TPE1" in audio.tags and audio.tags["TPE1"].text:
            uploader = str(audio.tags["TPE1"].text[0]).strip()
            if uploader and uploader.lower() != "unknown":
                return None, uploader
    except Exception:
        pass
    return None, None


def parse_one(path: Path) -> dict:
    """Parse a single mp3 by filename + optional yt-dlp metadata."""
    info, channel_hint = _load_info_and_channel(path)
    try:
        meta = parse_title(path.stem, info_meta=info)
    except TypeError:
        meta = parse_title(path.stem)
    meta["file"] = path.name
    if channel_hint:
        meta["channel_hint"] = channel_hint
    return meta


def _clean_llm_field(val) -> str | None:
    """Return stripped string, or None if LLM wants it cleared/empty (nullable)."""
    if val is None:
        return None
    s = str(val).strip()
    if s.lower() in ("", "null", "none", "n/a", "na", "unknown"):
        return None
    return s


def _index_llm_output(out: list, batch: list[dict]) -> dict:
    """Map LLM rows by id, with raw-title fallback."""
    by_id = {}
    by_raw = {}
    for row in out:
        if not isinstance(row, dict):
            continue
        if "id" in row and isinstance(row["id"], int):
            by_id[row["id"]] = row
        if "raw" in row:
            by_raw[row["raw"]] = row
    result = {}
    for idx, meta in enumerate(batch):
        row = by_id.get(idx) or by_raw.get(meta.get("raw"))
        if not row and len(out) == len(batch) and isinstance(out[idx], dict):
            row = out[idx]
        if row:
            result[idx] = row
    return result


def _apply_row(meta: dict, llm_row: dict):
    """Merge one LLM row into meta.

    Album: use LLM value only if it looks like a real album name.
    Otherwise always fall back to title (best guess when unsure).
    """
    title = _clean_llm_field(llm_row.get("title"))
    artist = _clean_llm_field(llm_row.get("artist"))
    album = _clean_llm_field(llm_row.get("album"))

    meta["title"] = title or ""
    meta["artist"] = artist or ""

    # Noise that means the "album" is really a filename / video title
    noise = ("official", "music video", "mv", "完整版", "官方", "高清", "歌词版", "lyric video")
    def _messy(s: str) -> bool:
        if not s:
            return True
        low = s.lower()
        if any(n in low for n in noise):
            return True
        if len(s) > 60:
            return True
        return False

    # Prefer a clean LLM album; otherwise copy title
    if album and not _messy(album):
        meta["album"] = album
    else:
        meta["album"] = title or ""

    if isinstance(llm_row.get("featured"), list):
        meta["featured"] = [str(x).strip() for x in llm_row["featured"] if x and str(x).strip()]
    else:
        meta["featured"] = []

    meta["confidence"] = "medium" if (title or artist) else "low"
    meta["needs_llm"] = False
    meta["llm_notes"] = llm_row.get("notes") or "llm_ok"
    meta["llm_authoritative"] = True


def apply_llm_batch(batch: list[dict]) -> list[dict]:
    """Two-pass LLM tagging.

    Pass 1: title only (no search). Fill what the model is confident about.
    Pass 2: only for items where title or artist is still empty — search then re-ask.
    Album falls back to title when missing.
    If the LLM call fails, items are marked llm_failed and should not be written.
    """
    if not batch:
        return batch
    print(f"LLM batch: {len(batch)} items (title-only first)")

    # ---- Pass 1: title only ----
    out = _call_llm(batch, use_search=False)
    if not out:
        for meta in batch:
            meta["needs_llm"] = False
            meta["llm_notes"] = "llm_failed"
            meta["llm_authoritative"] = False
        return batch

    rows = _index_llm_output(out, batch)
    unsure = []
    for idx, meta in enumerate(batch):
        row = rows.get(idx)
        if not row:
            meta["needs_llm"] = False
            meta["llm_notes"] = "llm_row_missing"
            meta["llm_authoritative"] = False
            continue
        _apply_row(meta, row)
        # Unsure if title or artist is still empty
        if not meta.get("title") or not meta.get("artist"):
            unsure.append((idx, meta))

    # ---- Pass 2: search only for unsure items ----
    if unsure:
        print(f"  {len(unsure)} items unsure — searching then re-asking")
        unsure_batch = [m for _, m in unsure]
        out2 = _call_llm(unsure_batch, use_search=True)
        if out2:
            rows2 = _index_llm_output(out2, unsure_batch)
            for local_idx, (orig_idx, meta) in enumerate(unsure):
                row2 = rows2.get(local_idx)
                if row2:
                    _apply_row(meta, row2)
                    meta["llm_notes"] = (row2.get("notes") or "llm_ok") + " (with search)"

    return batch


def finalize_allowlist(results: list[dict]):
    names = set()
    for m in results:
        if m.get("artist"):
            names.add(m["artist"])
        for f in m.get("featured") or []:
            names.add(f)
    _update_allowlist(names)


def parse_all():
    """Legacy sequential entry point."""
    results = []
    llm_queue = []
    for f in sorted(STAGING_DIR.glob("*.mp3")):
        meta = parse_one(f)
        if meta["needs_llm"]:
            llm_queue.append(meta)
        results.append(meta)

    for i in range(0, len(llm_queue), LLM_BATCH_SIZE):
        apply_llm_batch(llm_queue[i:i + LLM_BATCH_SIZE])

    finalize_allowlist(results)
    return results
