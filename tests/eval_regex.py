"""Measure the regex parsers on tests/regex_eval_cases.py.

Run: python tests/eval_regex.py   (uses an empty temp BASE_DIR; no network)
"""
import os, sys, tempfile, unicodedata
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("HOME", tempfile.mkdtemp())
import config
tmp = Path(tempfile.mkdtemp())
config.BASE_DIR = tmp; config.LOCAL_DB_PATH = tmp / "db.sqlite"; config.ALLOWLIST_PATH = tmp / "allow.json"
import local_db; local_db.LOCAL_DB_PATH = config.LOCAL_DB_PATH
import regex_parser
if hasattr(regex_parser, "ALLOWLIST_PATH"): regex_parser.ALLOWLIST_PATH = config.ALLOWLIST_PATH
local_db.init_db()
from tests.regex_eval_cases import CASES

def n(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return "".join(c for c in s if c.isalnum())

def grade(got_a, got_t, exp_a, exp_t):
    if not got_a or not got_t:
        return "abstain"
    if not exp_a:  # non-song / no artist known: any confident answer is wrong
        return "wrong"
    return "correct" if n(got_a) == n(exp_a) and n(got_t) == n(exp_t) else "wrong"

def run():
    import main
    res = {"from_filename": [], "parse_title": []}
    rows = []
    for raw, ea, et in CASES:
        p = main._parse_filename_artist_title(raw)
        g1 = grade(*(p or ("", "")), ea, et)
        m = regex_parser.parse_title(raw)
        g2 = grade(m.get("artist"), m.get("title"), ea, et)
        # In download mode only needs_llm=False results are trusted without LLM
        trusted_wrong = g2 == "wrong" and not m.get("needs_llm")
        res["from_filename"].append(g1)
        res["parse_title"].append((g2, m.get("needs_llm"), trusted_wrong))
        rows.append((raw, p, (m.get("artist"), m.get("title"), m.get("confidence"), m.get("needs_llm")), g1, g2))
    return res, rows

if __name__ == "__main__":
    res, rows = run()
    for r in rows:
        print(f"{r[3]:8} {r[4]:8} | {r[0]!r}\n           ff={r[1]}  pt={r[2]}")
    ff = res["from_filename"]; pt = [x[0] for x in res["parse_title"]]
    N = len(ff)
    for name, g in (("from-filename (--process step 2)", ff), ("parse_title (download mode)", pt)):
        print(f"\n{name}: n={N} correct={g.count('correct')} wrong={g.count('wrong')} abstain={g.count('abstain')}")
    print("parse_title wrong but needs_llm=False (would skip LLM):", sum(x[2] for x in res["parse_title"]))
    print("parse_title sent to LLM:", sum(1 for x in res["parse_title"] if x[1]))
