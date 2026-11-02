"""Merge duplicate artist spellings ("Jay Chou" / "周杰倫 Jay Chou" / "周杰伦").

A name is split into parts (CJK run, Latin run, bracketed alias). Each part is folded
(NFKC, Traditional->Simplified when opencc is installed, lowercase, alnum only) and
names sharing any folded part are grouped. The canonical name of a group is:
  1. a bilingual form (CJK + Latin) if one exists, else any form;
  2. among those, the most used; ties -> longest, then alphabetical.
Original script is kept (no conversion is written). The proposed map is written to
ARTIST_ALIASES_FILE (JSON {variant: canonical}); edit it and it is used as-is.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from match_guard import fold

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_LAT = re.compile(r"[A-Za-z]")
_MULTI = re.compile(r"\s*(?:,|、|&|/|⧸|\||｜| x | feat\.? | ft\.? )\s*", re.I)
_JUNK = re.compile(r"\s[-–]\s|[【】《》#:：]")
_CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af·]+")
_LAT_RUN = re.compile(r"[A-Za-z][A-Za-z0-9 .'\-]*[A-Za-z0-9.]|[A-Za-z]{2,}")


def split_name(name: str) -> tuple[list[str], list[str]] | None:
    """(cjk parts, latin parts) folded, or None for multi-artist / junk credits."""
    name = (name or "").strip()
    if not name or _MULTI.search(name) or _JUNK.search(name):
        return None
    base = re.sub(r"[(（][^)）]*[)）]", " ", name)  # bracket alias does not drive grouping
    cjk = [fold(r) for r in _CJK_RUN.findall(base)]
    lat = [fold(r) for r in _LAT_RUN.findall(base)]
    cjk = [c for c in cjk if c]
    lat = [l for l in lat if len(l) >= 2]
    if len(cjk) > 1 or len(lat) > 1:
        return None  # "陈昊宇 庄达菲": several people
    return cjk, lat


def _bilingual(n: str) -> bool:
    return bool(_CJK.search(n) and _LAT.search(n))


def build(counts: Counter) -> list[dict]:
    """counts: artist name -> number of files. Returns groups with >1 spelling.

    Anchor = folded CJK name when present. A Latin-only name joins a CJK anchor only
    when exactly one CJK anchor appears with it in bilingual names; otherwise it is
    grouped only with other spellings of itself.
    """
    sp = {n: split_name(n) for n in counts}
    lat_to_cjk = defaultdict(set)
    for n, v in sp.items():
        if v and v[0] and v[1]:
            lat_to_cjk[v[1][0]].add(v[0][0])
    groups = defaultdict(list)
    for n, v in sp.items():
        if not v:
            continue
        cjk, lat = v
        if cjk:
            key = "c:" + cjk[0]
        elif lat and len(lat_to_cjk.get(lat[0], ())) == 1:
            key = "c:" + next(iter(lat_to_cjk[lat[0]]))
        elif lat:
            key = "l:" + lat[0]
        else:
            continue
        groups[key].append(n)
    out = []
    for names in groups.values():
        if len(names) < 2:
            continue
        pool = [n for n in names if _bilingual(n)] or names
        canon = sorted(pool, key=lambda n: (-counts[n], -len(n), n))[0]
        out.append({"canonical": canon, "variants": sorted(names, key=lambda n: -counts[n]),
                    "files": sum(counts[n] for n in names)})
    out.sort(key=lambda g: -g["files"])
    return out


def to_map(groups: list[dict]) -> dict[str, str]:
    return {v: g["canonical"] for g in groups for v in g["variants"] if v != g["canonical"]}


def load(path: Path) -> dict[str, str]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(path: Path, mapping: dict[str, str]):
    Path(path).write_text(json.dumps(dict(sorted(mapping.items())), ensure_ascii=False, indent=1), encoding="utf-8")
