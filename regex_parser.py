"""Regex-first metadata parser for YouTube video titles.

Priority:
1. Strip YouTube video/channel noise (Official MV, Lyrics, HD, 4K, 完整版, - Topic)
2. Extract cover / feat / ft markers
3. Protect hyphenated artist names (A-Lin, G-Dragon, j-hope, i-dle)
4. Match structural patterns (《Title》, A - B, Artist: Title, Title by Artist)
5. Cross-check both sides against ARTIST_QUERIES + allowlist + local_db to auto-orient
   Artist vs Title without wasting LLM calls.

Returns dict: title, artist (primary), featured (list), album, confidence, needs_llm, raw.
"""
import json
import re
from config import ALLOWLIST_PATH, ARTIST_QUERIES
from local_db import connect, lookup_artist, lookup_track, normalize

# Hyphenated artists that must not be split on '-'
HYPHENATED_ARTISTS = {
    "a-lin": "A-Lin",
    "g-dragon": "G-Dragon",
    "i-dle": "(G)I-DLE",
    "(g)i-dle": "(G)I-DLE",
    "j-hope": "j-hope",
    "t-ara": "T-ara",
    "c-block": "C-BLOCK",
    "jay-z": "Jay-Z",
    "ne-yo": "Ne-Yo",
    "t-pain": "T-Pain",
}

SEED_ARTISTS = {normalize(name): name for name, _, _ in ARTIST_QUERIES}
SEED_ARTISTS.update(HYPHENATED_ARTISTS)

# Bracketed noise: (Official Video), [Official Audio], 【官方完整版MV】, (Lyrics), etc.
BRACKET_NOISE_RE = re.compile(
    r"[\(\[\{\u3010\u300c\u300e]\s*("
    r"official\s*(music\s*)?(video|audio|mv|lyric\s*video|visualizer|hd|4k)?|"
    r"mv|lyric\s*video|lyrics?|audio|visualizer|hd|4k|1080p|hq|"
    r"完整版|官方(完整版|高清|歌词版|mv|音源|正式版)?|高音质|动态歌词(版)?|歌词(版)?|纯享(版)?|现场(版)?|"
    r"live(\s*version|\s*performance)?|remastered(\s*\d{4})?|explicit|clean|"
    r"ost|主题曲|片尾曲|插曲|推广曲|概念曲"
    r")\s*[\)\]\}\u3011\u300d\u300f]",
    re.I,
)

# Unbracketed trailing noise: "... | Official MV", "... - Official Audio", "... MV"
TRAILING_NOISE_RE = re.compile(
    r"(?:\s*[-–—|｜/]\s*(?:official\s*(?:music\s*)?(?:video|audio|mv|lyric\s*video)|mv|lyrics?\s*video|高清完整版|官方mv|完整版)"
    r"|\s+\b(?:official\s*(?:music\s*)?(?:video|audio|mv)|mv|hd|4k)\b)\s*$",
    re.I,
)

# Cover pattern: "歌名 (Cover by 歌手)" / "歌名【翻唱：歌手】"
COVER_RE = re.compile(
    r"[\(\[\u3010]\s*(?:cover\s*by|covered\s*by|翻唱(?:自)?|cover)\s*[:：]?\s*(?P<cover_artist>[^\)\]\u3011]+)[\)\]\u3011]",
    re.I,
)

# Featured pattern inside brackets or trailing: (feat. X), [ft. Y], feat. Z
FEAT_EXTRACT_RE = re.compile(
    r"[\(\[\u3010]?\b(?:feat\.?|ft\.?|featuring|with)\s+(?P<feat>[^\)\]\u3011\-–—|｜/]+)[\)\]\u3011]?",
    re.I,
)

MULTI_ARTIST_RE = re.compile(
    r"\b(?:feat\.?|ft\.?|featuring|with|\u0026|and|&)\b"
    r"|\u002b|\uFF0B"          # plus signs
    r"|[/,，、×]",              # separators
    re.I,
)

NOISE_RE = re.compile(
    r"\b(?:official|audio|video|mv|lyrics?|live|concert|hd|4k|hq|topic|channel|records|entertainment)\b"
    r"|官方|完整版|歌词版|纯享版|高音质|音源",
    re.I,
)

# Chinese book-title pattern: 歌手《歌名》 or 《歌名》歌手
GUILLEMET_RE = re.compile(r"^(?P<pre>.*?)《(?P<title>[^》]+)》(?P<post>.*)$")

# Standard structural patterns (after hyphen protection)
PATTERNS = [
    # "A - B" / "A – B" / "A — B"
    re.compile(r"^(?P<left>.+?)\s*[-–—]\s*(?P<right>.+?)$"),
    # "Artist: Title" / "Artist：Title"
    re.compile(r"^(?P<left>.+?)\s*[:：]\s*(?P<right>.+?)$"),
    # "Title by Artist"
    re.compile(r"^(?P<title>.+?)\s+\bby\s+(?P<artist>.+?)$", re.I),
]


def _load_allowlist() -> set[str]:
    if ALLOWLIST_PATH.exists():
        try:
            return set(json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _protect_hyphens(text: str) -> tuple[str, dict[str, str]]:
    placeholders = {}
    out = text
    for idx, (low_k, canon_v) in enumerate(HYPHENATED_ARTISTS.items()):
        pat = re.compile(r"(?<!\w)" + re.escape(low_k) + r"(?!\w)", re.I)
        if pat.search(out):
            ph = f"__HYPH{idx}__"
            placeholders[ph] = canon_v
            out = pat.sub(ph, out)
    return out, placeholders


def _restore_hyphens(text: str, placeholders: dict[str, str]) -> str:
    if not text:
        return text
    for ph, canon_v in placeholders.items():
        text = text.replace(ph, canon_v)
    return text


def _split_artists(raw: str) -> list[str]:
    """Split a raw artist string on multi-artist separators while preserving hyphenated names."""
    if not raw:
        return []
    prot, ph = _protect_hyphens(raw)
    parts = MULTI_ARTIST_RE.split(prot)
    return [_restore_hyphens(p.strip(), ph) for p in parts if p.strip()]


def _strip_noise(raw: str) -> tuple[str, list[str], str | None]:
    """Strip bracket/trailing noise and extract featured/cover artists."""
    s = raw.strip()
    # Normalize full-width separators often produced by yt-dlp filename sanitization
    s = s.replace("：", ":").replace("｜", "|").replace("／", "/")

    # Extract cover artist if explicitly marked
    cover_artist = None
    m_cov = COVER_RE.search(s)
    if m_cov:
        cover_artist = m_cov.group("cover_artist").strip()
        s = COVER_RE.sub("", s).strip()

    # Extract explicit feat./ft. blocks
    extra_feats = []
    def _feat_sub(m):
        extra_feats.extend(_split_artists(m.group("feat")))
        return ""
    s = FEAT_EXTRACT_RE.sub(_feat_sub, s).strip()

    # Repeatedly strip bracketed and trailing noise
    prev = None
    while prev != s:
        prev = s
        s = BRACKET_NOISE_RE.sub("", s).strip()
        s = TRAILING_NOISE_RE.sub("", s).strip()

    return s, extra_feats, cover_artist


def _is_known_artist(name: str, db_conn, allowlist: set[str]) -> bool:
    if not name:
        return False
    parts = _split_artists(name)
    primary = parts[0] if parts else name
    n = normalize(primary)
    if not n:
        return False
    if n in SEED_ARTISTS or n in allowlist:
        return True
    return lookup_artist(db_conn, n) is not None


def _clean_quotes(s: str) -> str:
    return s.strip(" \"'「」『』《》【】[]()")


def _validate(title: str, artist: str) -> bool:
    """Return True if suspicious (should go to LLM)."""
    if not title or not artist:
        return True
    if NOISE_RE.search(artist) or NOISE_RE.search(title):
        return True
    if len(artist) > 35 or len(title) > 80:
        return True
    return False


def parse_title(raw_title: str) -> dict:
    """Parse a raw YouTube title. Returns metadata dict."""
    cleaned, extra_feats, cover_artist = _strip_noise(raw_title)
    allowlist = _load_allowlist()

    title, artist, featured = None, None, list(extra_feats)
    db = connect()
    try:
        # 1. Check Chinese《Title》pattern first (unambiguous title inside 《》)
        m_guil = GUILLEMET_RE.match(cleaned)
        if m_guil:
            t_cand = _clean_quotes(m_guil.group("title"))
            pre = _clean_quotes(m_guil.group("pre").strip(" -–—:：|"))
            post = _clean_quotes(m_guil.group("post").strip(" -–—:：|"))
            a_raw = pre or post
            if pre and post:
                a_raw = post if _is_known_artist(post, db, allowlist) and not _is_known_artist(pre, db, allowlist) else pre
            if t_cand and a_raw:
                split_a = _split_artists(a_raw)
                artist = split_a[0]
                featured.extend(split_a[1:])
                title = t_cand

        # 2. Check dash / colon / "by" patterns with hyphen protection
        if not (title and artist):
            prot, ph = _protect_hyphens(cleaned)
            for pat in PATTERNS:
                m = pat.match(prot)
                if not m:
                    continue
                gd = m.groupdict()
                if "title" in gd and "artist" in gd:
                    title = _clean_quotes(_restore_hyphens(gd["title"], ph))
                    a_raw = _clean_quotes(_restore_hyphens(gd["artist"], ph))
                    split_a = _split_artists(a_raw)
                    artist = split_a[0] if split_a else a_raw
                    featured.extend(split_a[1:])
                else:
                    left = _clean_quotes(_restore_hyphens(gd["left"], ph))
                    right = _clean_quotes(_restore_hyphens(gd["right"], ph))
                    left_known = _is_known_artist(left, db, allowlist)
                    right_known = _is_known_artist(right, db, allowlist)

                    # Auto-orient Artist vs Title based on known artist match
                    if left_known and not right_known:
                        a_raw, title = left, right
                    elif right_known and not left_known:
                        title, a_raw = left, right
                    else:
                        # If neither or both are known, check if one side is a known track
                        left_is_track = len(lookup_track(db, left)) > 0
                        right_is_track = len(lookup_track(db, right)) > 0
                        if right_is_track and not left_is_track:
                            a_raw, title = left, right
                        else:
                            # Default fallback: Title - Artist (will be flagged for LLM if unknown)
                            title, a_raw = left, right

                    split_a = _split_artists(a_raw)
                    artist = split_a[0] if split_a else a_raw
                    featured.extend(split_a[1:])
                break

        # If a cover artist was extracted, make cover artist primary and original artist featured
        if cover_artist:
            cov_split = _split_artists(cover_artist)
            if artist and normalize(artist) != normalize(cov_split[0]):
                featured.insert(0, artist)
            artist = cov_split[0]
            featured.extend(cov_split[1:])
            if not title:
                title = _clean_quotes(cleaned)

        # Deduplicate featured list while preserving order
        seen_f = {normalize(artist)} if artist else set()
        dedup_feat = []
        for f in featured:
            nf = normalize(f)
            if nf and nf not in seen_f:
                seen_f.add(nf)
                dedup_feat.append(f)
        featured = dedup_feat

        # 3. Validation & Confidence scoring
        suspicious = False
        if title and artist:
            suspicious = _validate(title, artist)
            a_hit = _is_known_artist(artist, db, allowlist)
            t_hit = len(lookup_track(db, title, artist if a_hit else None)) > 0
            if a_hit and not suspicious:
                confidence = "high"
            elif a_hit:
                confidence = "medium"
            elif t_hit and not suspicious:
                confidence = "medium"
            else:
                confidence = "low"
                suspicious = True
        else:
            confidence = "none"
            suspicious = True

        return {
            "title": title,
            "artist": artist,
            "featured": featured,
            "album": None,
            "confidence": confidence,
            "needs_llm": suspicious or confidence in ("none", "low"),
            "raw": raw_title,
        }
    finally:
        db.close()