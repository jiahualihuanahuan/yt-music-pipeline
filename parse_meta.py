"""Metadata parsing orchestrator.

1. regex_parser (+ .info.json inspection) on every file
2. collect needs_llm items into batches
3. send batches to local Qwen 3.5 (OpenAI-compatible) with <think> stripping & ID alignment
4. merge + validate results, update allowlist
"""
import json
import re
import time
import requests
from pathlib import Path
from config import (
    STAGING_DIR, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT,
    LLM_BATCH_SIZE, LLM_RETRIES, ALLOWLIST_PATH,
)
from regex_parser import parse_title
from local_db import connect, lookup_artist, normalize

SYSTEM_PROMPT = """You are a music metadata expert. Given a JSON array of items containing a YouTube video title ("raw") and optional uploader channel ("channel"), extract the song title and primary artist.

Rules:
- ALWAYS return a non-empty "title" and "artist" for every input "id".
- Keep the exact "id" integer from the input object so results can be matched.
- Chinese titles: the part AFTER the dash/colon is often the artist and BEFORE is the title, OR Artist《Title》. Use your knowledge of Chinese/Asian/Western artists and the "channel" hint to pick accurately.
- "A-Lin", "G-Dragon", "j-hope", "(G)I-DLE" style hyphenated names are ONE artist, do not split.
- If multiple artists appear (feat/ft/with/翻唱/cover/原唱/&/、), put the primary performer in "artist" and others in "featured" (array of strings).
- Strip noise like "Official Music Video", "MV", "完整版", "歌词版", "Lyrics", "4K", "HD" from "title".
- "album" is optional: set to null unless an album name is explicitly stated in the title.
- Respond with ONLY a valid JSON array, no markdown fences, no commentary:
  [{"id": 0, "title": "...", "artist": "...", "featured": [], "album": null, "notes": "..."}]
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


def _call_llm(batch: list[dict]) -> list[dict] | None:
    req_items = []
    for idx, b in enumerate(batch):
        item = {"id": idx, "raw": b["raw"]}
        if b.get("channel_hint"):
            item["channel"] = b["channel_hint"]
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
    last_err = None
    for attempt in range(1, LLM_RETRIES + 1):
        try:
            r = requests.post(f"{LLM_BASE_URL}/chat/completions", json=payload, timeout=timeout)
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
    db = connect()
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
        ALLOWLIST_PATH.write_text(json.dumps(sorted(existing), ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"allowlist +{len(added)}: {sorted(added)[:10]}")


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

    # Fallback: read TPE1 (uploader/channel) written by yt-dlp --add-metadata
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

def apply_llm_batch(batch: list[dict]) -> list[dict]:
    """Send one batch to LLM and merge results back into the meta dicts by ID or raw match."""
    if not batch:
        return batch
    print(f"LLM batch: {len(batch)} items")
    out = _call_llm(batch)
    if not out:
        for meta in batch:
            meta["needs_llm"] = False
            meta["llm_notes"] = "llm_failed"
        return batch

    # Index LLM output by 'id' and fallback 'raw'
    by_id = {}
    by_raw = {}
    for row in out:
        if not isinstance(row, dict):
            continue
        if "id" in row and isinstance(row["id"], int):
            by_id[row["id"]] = row
        if "raw" in row:
            by_raw[row["raw"]] = row

    for idx, meta in enumerate(batch):
        llm_row = by_id.get(idx) or by_raw.get(meta["raw"])
        if not llm_row and len(out) == len(batch) and isinstance(out[idx], dict):
            llm_row = out[idx]

        if llm_row:
            if llm_row.get("title"):
                meta["title"] = str(llm_row["title"]).strip()
            if llm_row.get("artist"):
                meta["artist"] = str(llm_row["artist"]).strip()
            if isinstance(llm_row.get("featured"), list):
                meta["featured"] = [str(x).strip() for x in llm_row["featured"] if x]
            if llm_row.get("album") and str(llm_row["album"]).lower() not in ("null", "none", ""):
                meta["album"] = str(llm_row["album"]).strip()
            meta["confidence"] = "medium"
            meta["needs_llm"] = False
            meta["llm_notes"] = llm_row.get("notes") or "llm_ok"
        else:
            meta["needs_llm"] = False
            meta["llm_notes"] = "llm_row_missing"

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