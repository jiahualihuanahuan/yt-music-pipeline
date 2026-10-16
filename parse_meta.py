"""Metadata parsing orchestrator.

1. regex_parser on every file
2. collect needs_llm items into batches
3. send batches to local Qwen (OpenAI-compatible)
4. merge + validate results, update allowlist
"""
import json
import re
import requests
from pathlib import Path
from config import (STAGING_DIR, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT,
                    LLM_BATCH_SIZE, ALLOWLIST_PATH)
from regex_parser import parse_title
from local_db import connect, lookup_artist, upsert_artist, normalize

SYSTEM_PROMPT = """You are a music metadata expert. Given a YouTube video title,
extract: song title, primary artist, featured artists (if any), album (if any).
Rules:
- Chinese titles: the part AFTER the dash/colon is usually the artist, BEFORE is the title.
- "A-Lin" style hyphenated names are ONE artist, do not split.
- If two or more artist names appear separated by feat/ft/with/by/翻唱/cover/原唱/&/,,
  list all of them; the first is primary.
- If the title looks like it could be an artist name instead, say so in notes.
- Respond with ONLY a JSON array, no markdown fences. Each object:
  {"raw": "...", "title": "...", "artist": "...", "featured": [...], "album": "...", "notes": "..."}
- If unsure about a field, use null. Never invent data.
"""

def _call_llm(batch):
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(
                [{"raw": b["raw"]} for b in batch], ensure_ascii=False)},
        ],
        "temperature": 0.1,
    }
    try:
        r = requests.post(f"{LLM_BASE_URL}/chat/completions",
                          json=payload, timeout=LLM_TIMEOUT)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        # strip possible markdown fences
        content = re.sub(r"^```(?:json)?\s*", "", content.strip())
        content = re.sub(r"\s*```$", "", content)
        return json.loads(content)
    except Exception as e:
        print(f"LLM batch failed: {e}")
        return None


def _update_allowlist(artist_names):
    existing = set()
    if ALLOWLIST_PATH.exists():
        existing = set(json.loads(ALLOWLIST_PATH.read_text()))
    added = set()
    db = connect()
    for name in artist_names:
        n = normalize(name)
        if not n or n in existing:
            continue
        if lookup_artist(db, n):
            existing.add(n)
            added.add(n)
    if added:
        ALLOWLIST_PATH.write_text(json.dumps(sorted(existing), ensure_ascii=False, indent=1))
        print(f"allowlist +{len(added)}: {sorted(added)[:10]}")


def parse_all():
    results = []
    llm_queue = []
    for f in sorted(STAGING_DIR.glob("*.mp3")):
        meta = parse_title(f.stem)
        meta["file"] = f.name
        if meta["needs_llm"]:
            llm_queue.append(meta)
        results.append(meta)

    # batch LLM
    for i in range(0, len(llm_queue), LLM_BATCH_SIZE):
        batch = llm_queue[i:i + LLM_BATCH_SIZE]
        print(f"LLM batch {i//LLM_BATCH_SIZE + 1}: {len(batch)} items")
        out = _call_llm(batch)
        if out and len(out) == len(batch):
            for meta, llm_row in zip(batch, out):
                if llm_row.get("title"):
                    meta["title"] = llm_row["title"]
                if llm_row.get("artist"):
                    meta["artist"] = llm_row["artist"]
                if llm_row.get("featured"):
                    meta["featured"] = llm_row["featured"]
                if llm_row.get("album"):
                    meta["album"] = llm_row["album"]
                meta["needs_llm"] = False
                meta["llm_notes"] = llm_row.get("notes")

    # collect all artist names for allowlist
    names = set()
    for m in results:
        if m.get("artist"):
            names.add(m["artist"])
        for f in m.get("featured") or []:
            names.add(f)
    _update_allowlist(names)
    return results
