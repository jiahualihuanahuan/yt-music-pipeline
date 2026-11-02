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
# Same channels, title may carry a (version) / (drama insert) note and junk may follow:
# "Artist - Title (電視劇《X》插曲)『lyric』【動態歌詞MV】♫·流行音樂 hit china"
_LYRIC_LOOSE_RE = re.compile(r"^(?P<a>[^『』【】《》()]+?)\s+-\s+(?P<t>[^『』【】]+?)\s*『[^』]{4,}』")
# "【HD】Artist - Title [歌詞字幕][完整高清音質] English - Translation"
_HD_RE = re.compile(r"^【HD】\s*(?P<a>[^-【】\[\]]+?)\s+-\s+(?P<t>[^\[\]【】]+?)\s*\[(?:新歌|歌詞字幕)")
# "Artist - Title【歌詞字幕 / 完整高清音質】♫「lyric...」English - Translation"
_QUOTE_RE = re.compile(r"^(?P<a>[^「」【】《》()]+?)\s+-\s+(?P<t>[^「」【】《》()]+?)\s*【[^】]*(?:歌詞|歌词)[^】]*】\s*[♫♪]?\s*「[^」]*」")
# parentheticals that describe the source (drama / movie / OST), not the recording
_SOURCE_PAREN_RE = re.compile(r"\s*[(（][^()（）]*(《|劇|剧|插曲|主題曲|主题曲|片頭曲|片尾曲|片头曲|ost|theme)[^()（）]*[)）]", re.I)
_CJK_NAME = re.compile(r"^[\u4e00-\u9fff·]{2,4}$")

# Cover uploads: "【Performer】…TITLE-Cover：Original" or "【Performer】…【TITLE】…cover：Original"
_COVER_HEAD_RE = re.compile(r"^【(?P<p>[^】]+)】(?P<rest>.+)$")
_COVER_ORIG_RE = re.compile(r"(?:cover|原唱|翻自)\s*[:：]\s*(?P<o>[^【】()（）|]+)", re.I)
# "Artist《Title》Official MV" with nothing else
# Only 《》 (「」 is often a lyric quote) and only with an explicit Official marker:
# without it the left side is often a clickbait phrase or channel ("孟慧圆直播唱《喜欢你》").
_GUILLEMET_RE = re.compile(r"^(?P<a>[^《》【】\s!?！？,，。:：\-–][^《》【】!?！？,，。:：–]{0,24}?)\s*《(?P<t>[^》]+)》\s*official\s+(mv|audio|music\s*video|video)\s*$", re.I)
# Latin "Artist - Title (… Official Video/Audio)"
_OFFICIAL_RE = re.compile(r"^(?P<a>[^-–()\[\]]+?)\s+[-–]\s+(?P<t>[^()\[\]]+?)\s*(\((from|feat)[^)]*\)\s*)?\((official\s+(music\s+)?(video|audio))\)\s*$", re.I)

_BAD_ARTIST = re.compile(r"\||流行歌曲|热门|熱門|翻唱|cover|合集|专辑|專輯|直播|電音|电音|edm|disney|vevo|playlist|cast\b|various artists|^the lion king|official", re.I)


def clean_raw(raw: str) -> str:
    s = unicodedata.normalize("NFKC", raw or "").translate(_SLASHES)
    s = VIDEO_ID_RE.sub("", s)
    s = re.sub(r"[♫♪]+\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _split(a: str) -> list[str]:
    a = re.sub(r"[(（][^)）]*[)）]", "", a).strip()
    parts = [p.strip() for p in re.split(r"\s*(?:/|、|,|&| x | ft\.? | feat\.? )\s*", a) if p.strip()]
    # "王蓉 龍梅子 崔子格": space-separated Chinese names (not "Jess 佳仙" / "夏天Alex")
    if len(parts) == 1 and " " in parts[0]:
        toks = parts[0].split()
        if len(toks) >= 2 and all(_CJK_NAME.match(t) for t in toks):
            parts = toks
    return parts


_ORIG_PAREN_RE = re.compile(r"\s*[(（]\s*(?:原唱|翻自|cover)\s*[:：]?\s*(?P<o>[^()（）]+)[)）]", re.I)
_SOURCE_BLOCK_RE = re.compile(r"\s*《[^》]*(劇|剧|曲|ost)[^》]*》\s*$", re.I)


def _clean_title(t: str) -> str:
    t = _ORIG_PAREN_RE.sub("", t)
    t = _SOURCE_BLOCK_RE.sub("", _SOURCE_PAREN_RE.sub("", t))
    t = re.sub(r'^["“”\']+|["“”\']+$', "", t.strip())
    return re.sub(r"\s+", " ", t).strip(" -–")


def _orig(t: str) -> str | None:
    m = _ORIG_PAREN_RE.search(t)
    return _split(m.group("o"))[0] if m and _split(m.group("o")) else None


_FEAT_RE = re.compile(r"\s*[(\[]?\s*\b(?:feat\.?|ft\.?|featuring)\s+(?P<f>[^)\]]+?)\s*[)\]]?\s*$", re.I)


def _title_feat(title: str) -> tuple[str, list[str]]:
    """'Girls Like You ft. Cardi B' -> ('Girls Like You', ['Cardi B']); strips 《》 around a title."""
    title = re.sub(r"^《(.+)》$", r"\1", title.strip())
    m = _FEAT_RE.search(title)
    if m and m.start() > 0:
        return title[: m.start()].strip(), _split(m.group("f"))
    return title, []


def parse(raw: str) -> dict | None:
    s = clean_raw(raw)
    s_nfkc = s
    m = _LYRIC_RE.match(s_nfkc)
    if m:
        artists = _split(m.group("a"))
        if artists and not _BAD_ARTIST.search(artists[0]):
            return {"artist": artists[0], "title": m.group("t").strip(), "featured": artists[1:],
                    "original_artist": None, "rule": "lyric_channel"}
    for rule, rx in (("lyric_channel", _LYRIC_LOOSE_RE), ("hd_lyric", _HD_RE), ("quote_lyric", _QUOTE_RE)):
        m = rx.match(s_nfkc)
        if m:
            artists = _split(m.group("a"))
            title = _clean_title(m.group("t"))
            if artists and title and not _BAD_ARTIST.search(m.group("a")):
                return {"artist": artists[0], "title": title, "featured": artists[1:],
                        "original_artist": _orig(m.group("t")), "rule": rule}
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
                return {"artist": performer, "title": title.strip(" -–"), "featured": [],
                        "original_artist": orig[0] if orig else None, "rule": "cover"}
    m = _GUILLEMET_RE.match(s_nfkc)
    if m:
        a = m.group("a").strip()
        if not _BAD_ARTIST.search(a) and not re.search(r"\s-|-\s|-$", a):
            return {"artist": _split(a)[0], "title": _clean_title(m.group("t")), "featured": _split(a)[1:],
                    "original_artist": None, "rule": "guillemet"}
    m = _OFFICIAL_RE.match(s_nfkc)
    if m:
        artists = _split(m.group("a"))
        if artists and not _BAD_ARTIST.search(m.group("a")):
            title, feat = _title_feat(_clean_title(m.group("t")))
            return {"artist": artists[0], "title": title, "featured": artists[1:] + feat,
                    "original_artist": None, "rule": "official"}
    return None
