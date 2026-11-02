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
from title_rules import clean_raw
from local_db import connect, lookup_artist, normalize

SYSTEM_PROMPT = """You are a music metadata expert tagging a personal library ripped from YouTube.
Input: a JSON array of items. Each item may have:
- "id": integer, echo it back unchanged
- "raw": the YouTube video title / filename (main signal)
- "channel": uploader channel (a HINT, usually NOT the artist)
- "folder": the folder the file sits in, often a YouTube playlist name such as
  "Mix - <artist>", "Top Songs - <artist>", "Uploads from <artist> - Topic",
  "Ultimate Disney Playlist". The folder is NOT the album.
- "existing_title", "existing_artist", "existing_album": current tags. Often wrong:
  they may hold the uploader, the movie name, lyric lines, or the ORIGINAL singer of a cover.
- "web_search": optional web snippets, only on a second pass.

Decide for every item:
1. is_music: false for anything that is not a song: talk / interview / livestream clips,
   vlogs, travel videos, news, podcasts, sports, school promo videos, cartoon episodes,
   "NA". For non-music return title=null, artist=null, album=null, is_music=false and a
   short reason in "notes". Never invent a song for it.
2. title: the song name only. Remove: 『lyric lines』, 【動態歌詞/Pinyin/Vietsub/Lyrics】,
   (Official Video/Audio/MV), [High Quality], HD, 4K, (English), (with lyrics), Sing-A-Long,
   完整版, [11-char video id], "(From <movie>)" and the clickbait sentence around a
   《》 or 【】 song name. Keep version words that matter: (Live), (Remix), (伴奏) -> keep
   "伴奏" as " (伴奏)" at the end of the title.
3. artist: the PERFORMER heard on the recording.
   - Not the uploader/channel (VEVO, "<Name> - Topic", fan channels like "Holy007",
     "DisneyMusicVEVO", lyric channels), not a movie/show name ("Aladdin", "Pocahontas",
     "Frozen"). For a soundtrack song where the singer is not in the raw text, give the
     credited singer only if you are sure; otherwise null.
   - Covers ("Cover：X", "原唱：X", "翻自：X", "翻唱"): artist = the cover performer
     (often the 【name】 at the start or the channel), original_artist = X.
   - Several artists split by "/", "⧸", "、", ",", "&", "feat.": first one in "artist",
     the rest in "featured".
   - Hyphenated names are one artist: "A-Lin", "Li-2c", "G-Dragon".
4. Keep the original language and script exactly as written in raw. Do NOT translate
   and do NOT convert Traditional <-> Simplified Chinese. A Chinese song keeps its Chinese
   title even if an English translation is also present in raw.
5. album: only if you know the real album; else null (the pipeline uses the title).
6. confidence: "high" (raw states it unambiguously), "medium" (you know the song), "low".
   Return null for any field you are not confident about rather than guessing.

Examples
raw "张三 - 夜空『想把星星都摘給你 我的夜空只有你』【動態歌詞Lyrics】" folder "Mix - 李四"
-> {"title":"夜空","artist":"张三","featured":[],"original_artist":null,"album":null,"is_music":true,"confidence":"high","notes":null}
raw "王五⧸赵六 - 心事『...』【動態歌詞Lyrics】"
-> {"title":"心事","artist":"王五","featured":["赵六"],"original_artist":null,"album":null,"is_music":true,"confidence":"high","notes":null}
raw "【小明】煙嗓翻唱【晴天】Cover：周杰倫" channel "小明"
-> {"title":"晴天","artist":"小明","featured":[],"original_artist":"周杰倫","album":null,"is_music":true,"confidence":"high","notes":"cover"}
raw "反差萌小姐姐翻唱《手心的蔷薇》" folder "小明的翻唱" channel "小明"
-> {"title":"手心的蔷薇","artist":"小明","featured":[],"original_artist":null,"album":null,"is_music":true,"confidence":"medium","notes":"cover"}
raw "周深《大鱼》Official Audio"
-> {"title":"大鱼","artist":"周深","featured":[],"original_artist":null,"album":null,"is_music":true,"confidence":"high","notes":null}
raw "Idina Menzel - Let It Go (from Frozen) (Official Video)" existing_artist "DisneyMusicVEVO"
-> {"title":"Let It Go","artist":"Idina Menzel","featured":[],"original_artist":null,"album":"Frozen","is_music":true,"confidence":"high","notes":"uploader was tagged as artist"}
raw "Pocahontas - Colors of the Wind HD" existing_artist "Pocahontas"
-> {"title":"Colors of the Wind","artist":"Judy Kuhn","featured":[],"original_artist":null,"album":"Pocahontas","is_music":true,"confidence":"medium","notes":"Pocahontas is the movie"}
raw "Dig a Little Deeper - Princess and the Frog"
-> {"title":"Dig a Little Deeper","artist":null,"featured":[],"original_artist":null,"album":"The Princess and the Frog","is_music":true,"confidence":"medium","notes":"title first, movie second"}
raw "Mix - 某歌手/Falling You" existing_artist "Céline Dion" existing_title "Falling into You"
-> {"title":"Falling You","artist":null,"featured":[],"original_artist":null,"album":null,"is_music":true,"confidence":"low","notes":"existing tags are a different song"}
raw "某歌手：我的巡演要开始了！" folder "某歌手合集"
-> {"title":null,"artist":null,"featured":[],"original_artist":null,"album":null,"is_music":false,"confidence":"high","notes":"talk clip"}
raw "Journey To Fansipan PART 1⧸ Vlog Review No. 367" folder "Top Songs of the Decade Playlist"
-> {"title":null,"artist":null,"featured":[],"original_artist":null,"album":null,"is_music":false,"confidence":"high","notes":"travel vlog"}
raw "Spidey and His Amazing Friends： Panther Patience [sHSgwpzqKQY]"
-> {"title":null,"artist":null,"featured":[],"original_artist":null,"album":null,"is_music":false,"confidence":"high","notes":"cartoon episode"}

Respond with ONLY a valid JSON array (no markdown, no commentary), one object per item:
[{"id":0,"title":...,"artist":...,"featured":[],"original_artist":...,"album":...,"is_music":true,"confidence":"high","notes":...}]
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
    """Ask what the artist, title, and album are for this track."""
    raw = (item.get("raw") or "").strip()
    raw = re.sub(r"[\(\[].*?(lyrics|official|mv|hd|4k).*?[\)\]]", "", raw, flags=re.I)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return ""
    return f"what is the artist, title and album of {raw[:100]}"


def _call_llm(batch: list[dict], use_search: bool = False) -> list[dict] | None:
    """Call the LLM. Search is only attached when use_search=True."""
    req_items = []
    for idx, b in enumerate(batch):
        item = {"id": idx, "raw": clean_raw(b["raw"])}
        if b.get("channel_hint"):
            item["channel"] = b["channel_hint"]
        if b.get("folder_hint"):
            item["folder"] = b["folder_hint"]
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
    import config as _cfg
    rules = None
    if getattr(_cfg, "USE_TITLE_RULES", True):
        from title_rules import parse as _rules_parse
        rules = _rules_parse(path.stem)
    if rules:
        meta = {"title": rules["title"], "artist": rules["artist"], "featured": rules["featured"],
                "original_artist": rules.get("original_artist"), "album": None,
                "confidence": "rules", "needs_llm": False, "raw": clean_raw(path.stem),
                "llm_notes": f"rules:{rules['rule']}"}
    elif not getattr(_cfg, "USE_REGEX_PARSER", False):
        # Regex parsing disabled (see tests/eval_regex.py): every file goes to the LLM.
        meta = {"title": None, "artist": None, "featured": [], "album": None,
                "confidence": "none", "needs_llm": True, "raw": clean_raw(path.stem)}
    else:
        try:
            meta = parse_title(path.stem, info_meta=info)
        except TypeError:
            meta = parse_title(path.stem)
    meta["file"] = path.name
    if channel_hint:
        meta["channel_hint"] = channel_hint
    return meta



import re as _re

# Known hyphenated / parenthesized artist names to preserve
_KNOWN_HYPHEN = {
    "a-lin", "g-dragon", "j-hope", "j-min", "(g)i-dle", "g)i-dle",
    "t-ara", "u-know", "nct 127", "nct dream",
}

# Keep apostrophes, periods, commas, & ! ? + so "You're Welcome", "Fire EX.", "What's This?" survive
_SPECIAL_RE = _re.compile(r"[^\w\s\-()'’.,&!?+\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", _re.UNICODE)
_MULTI_SPACE = _re.compile(r"\s+")


def _clean_text(val: str | None, kind: str = "title") -> str | None:
    """Strip special characters. Keep letters, numbers, CJK, spaces, and hyphens
    for known artist names like A-Lin / G-Dragon / (G)I-DLE.
    """
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None
    # Remove most specials but keep hyphen and parentheses
    s = _SPECIAL_RE.sub("", s)
    s = _MULTI_SPACE.sub(" ", s).strip(" -")
    if not s:
        return None
    low = s.lower()
    # For artist: if it contains a hyphen and isn't a known hyphenated name,
    # and looks like noise, drop the hyphenated junk. Otherwise keep.
    if kind == "artist" and "-" in s and low not in _KNOWN_HYPHEN:
        # Keep if both sides are short word-like (A-Lin style)
        parts = s.split("-")
        if not all(p.strip() and len(p.strip()) <= 12 for p in parts):
            s = s.replace("-", " ")
            s = _MULTI_SPACE.sub(" ", s).strip()
    return s or None


def _artist_is_known(name: str) -> bool:
    """True if artist is in the local DB or MusicBrainz."""
    if not name:
        return False
    try:
        from local_db import connect, lookup_artist
        db = connect()
        try:
            if lookup_artist(db, name):
                return True
        finally:
            db.close()
    except Exception:
        pass
    try:
        from musicbrainz import mb_artist
        if mb_artist(name):
            return True
    except Exception:
        pass
    return False


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
    title = _clean_text(_clean_llm_field(llm_row.get("title")), "title")
    artist = _clean_text(_clean_llm_field(llm_row.get("artist")), "artist")
    album = _clean_text(_clean_llm_field(llm_row.get("album")), "album")

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

    if isinstance(llm_row.get("original_artist"), str) and llm_row["original_artist"].strip():
        meta["original_artist"] = llm_row["original_artist"].strip()
    is_music = llm_row.get("is_music")
    if is_music is False or str(is_music).lower() == "false":
        meta.update(is_music=False, title="", artist="", album="", featured=[])
    else:
        meta["is_music"] = True
    conf = str(llm_row.get("confidence") or "").lower()
    if conf not in ("high", "medium", "low"):
        conf = "medium" if (title or artist) else "low"
    if conf == "low" and meta.get("is_music"):
        # don't write guesses; clear instead (pass 2 may retry with search)
        meta.update(title="", artist="", album="")
    meta["confidence"] = conf
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
        if meta.get("is_music") is not False and (not meta.get("title") or not meta.get("artist")):
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
