"""High-precision filename/title rules tuned on real library patterns.

`parse(raw)` returns {"artist", "title", "featured", "original_artist", "rule"} only
when the title matches one of a few unambiguous shapes; otherwise None (abstain),
and the caller should send the item to the LLM.

`clean_raw(raw)` normalizes YouTube junk (fullwidth punctuation, [videoId],
⧸ lookalike slashes, trailing ♫) and is safe to apply to anything.
"""
import re
import unicodedata

VIDEO_ID_RE = re.compile(r"\s*\[[A-Za-z0-9_-]{11}\]\s*$")
_SLASHES = str.maketrans({"⧸": "/", "∕": "/", "／": "/", "｜": "|"})

# Lyric-channel uploads: "Artist - Title『lyric line』【動態歌詞…】"
_LYRIC_RE = re.compile(r"^(?P<a>[^『』【】《》]+?)\s+-\s+(?P<t>[^『』【】《》()（）]+?)\s*『[^』]*』\s*(【[^】]*】\s*)*[♫♪\s]*$")
# Cover uploads: "【Performer】…TITLE-Cover：Original" or "【Performer】…【TITLE】…cover：Original"
_COVER_HEAD_RE = re.compile(r"^【(?P<p>[^】]+)】(?P<rest>.+)$")
_COVER_ORIG_RE = re.compile(r"(?:cover|原唱|翻自)\s*[:：]\s*(?P<o>[^【】()（）|]+)", re.I)
# "Artist《Title》Official MV" with nothing else
_GUILLEMET_RE = re.compile(r"^(?P<a>[^《》【】\s!?！？,，。:：][^《》【】!?！？,，。:：]{0,15}?)\s*[《「](?P<t>[^》」]+)[》」]\s*(official\s*(mv|audio|music\s*video)?|mv)?\s*$", re.I)
# Latin "Artist - Title (… Official Video/Audio)"
_OFFICIAL_RE = re.compile(r"^(?P<a>[^-–()\[\]]+?)\s+[-–]\s+(?P<t>[^()\[\]]+?)\s*(\((from|feat)[^)]*\)\s*)?\((official\s+(music\s+)?(video|audio))\)\s*$", re.I)

_BAD_ARTIST = re.compile(r"翻唱|cover|合集|disney|vevo|playlist|cast\b|various artists|^the lion king|official", re.I)


def clean_raw(raw: str) -> str:
    s = unicodedata.normalize("NFKC", raw or "").translate(_SLASHES)
    s = VIDEO_ID_RE.sub("", s)
    s = re.sub(r"[♫♪]+\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _split(a: str) -> list[str]:
    a = re.sub(r"[(（][^)）]*[)）]", "", a).strip()
    parts = [p.strip() for p in re.split(r"\s*(?:/|、|,|&| x | ft\.? | feat\.? )\s*", a) if p.strip()]
    return parts


def parse(raw: str) -> dict | None:
    s = clean_raw(raw)
    s_nfkc = s
    m = _LYRIC_RE.match(s_nfkc)
    if m:
        artists = _split(m.group("a"))
        if artists and not _BAD_ARTIST.search(artists[0]):
            return {"artist": artists[0], "title": m.group("t").strip(), "featured": artists[1:],
                    "original_artist": None, "rule": "lyric_channel"}
    m = _COVER_HEAD_RE.match(s_nfkc)
    if m:
        performer, rest = m.group("p").strip(), m.group("rest")
        o = _COVER_ORIG_RE.search(rest)
        if o:
            inner = [x for x in re.findall(r"【([^】]+)】", rest) if not re.search(r"[A-Za-z]{4,}", x)]
            title = None
            if len(set(inner)) == 1:
                title = inner[0]
            else:
                t = re.match(r"^\s*(?:\S*翻唱\s*)?(?P<t>[^\s【】!！?？:：]+?)\s*-?\s*(?:[(（]\s*)?cover", rest, re.I)
                if t and not inner:
                    title = t.group("t")
            if title:
                orig = _split(o.group("o"))
                return {"artist": performer, "title": title.strip(), "featured": [],
                        "original_artist": orig[0] if orig else None, "rule": "cover"}
    m = _GUILLEMET_RE.match(s_nfkc)
    if m:
        a = m.group("a").strip()
        if not _BAD_ARTIST.search(a):
            return {"artist": _split(a)[0], "title": m.group("t").strip(), "featured": _split(a)[1:],
                    "original_artist": None, "rule": "guillemet"}
    m = _OFFICIAL_RE.match(s_nfkc)
    if m:
        artists = _split(m.group("a"))
        if artists and not _BAD_ARTIST.search(m.group("a")):
            return {"artist": artists[0], "title": m.group("t").strip(), "featured": artists[1:],
                    "original_artist": None, "rule": "official"}
    return None
