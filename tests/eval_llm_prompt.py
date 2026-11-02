"""Evaluate title_rules + the LLM prompt on a tag-dump CSV of your own library.

Makes REAL LLM calls to LLM_BASE_URL from config / config_local.py. Nothing is
written to your music files. The CSV stays on your machine.

  python tests/eval_llm_prompt.py --csv sample.csv [--labels labels.json] [--limit 50] [--dry-run]

CSV needs columns: relative_path, filename (optional: artist, title, album).
labels.json (optional): {"<relative_path>": {"artist": str|null, "title": str|null,
  "is_music": true|false|null, "uncertain": bool}}. artist null = grade title only.
Without labels it just writes predictions to logs/llm_eval_<time>.csv for review.
"""
import argparse, csv, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config
import parse_meta as pm
import title_rules
from match_guard import fold


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--labels")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=config.LLM_BATCH_SIZE)
    ap.add_argument("--no-rules", action="store_true", help="send every row to the LLM")
    ap.add_argument("--dry-run", action="store_true", help="print the first request, call nothing")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.csv, encoding="utf-8-sig")))
    if a.limit:
        rows = rows[: a.limit]
    labels = json.load(open(a.labels, encoding="utf-8")) if a.labels else {}

    preds, llm_items = {}, []
    for r in rows:
        p = Path(r["relative_path"])
        stem = Path(r.get("filename") or p.name).stem
        rule = None if a.no_rules else title_rules.parse(stem)
        if rule:
            preds[r["relative_path"]] = {"artist": rule["artist"], "title": rule["title"], "is_music": True,
                                         "source": "rules:" + rule["rule"]}
        else:
            llm_items.append({"key": r["relative_path"], "raw": stem, "folder_hint": p.parent.name,
                              "existing_title": r.get("title") or "", "existing_artist": r.get("artist") or "",
                              "existing_album": r.get("album") or ""})
    print(f"{len(rows)} rows: {len(preds)} by rules, {len(llm_items)} to LLM ({config.LLM_BASE_URL}, {config.LLM_MODEL})")

    if a.dry_run:
        print(pm.SYSTEM_PROMPT[:400], "...")
        print(json.dumps(llm_items[:3], ensure_ascii=False, indent=1))
        return

    for i in range(0, len(llm_items), a.batch):
        batch = [dict(x) for x in llm_items[i:i + a.batch]]
        pm.apply_llm_batch(batch)  # pass 1 + SearXNG pass 2, same as --process
        for m in batch:
            preds[m["key"]] = {"artist": m.get("artist") or None, "title": m.get("title") or None,
                               "is_music": m.get("is_music"), "confidence": m.get("confidence"),
                               "source": "llm", "notes": m.get("llm_notes")}
        print(f"  {min(i + a.batch, len(llm_items))}/{len(llm_items)}")

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = config.LOG_DIR / f"llm_eval_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["relative_path", "source", "is_music", "artist", "title", "confidence", "label_artist", "label_title", "label_is_music", "verdict"])
        stats = {}
        for r in rows:
            k = r["relative_path"]; pr = preds.get(k, {}); lb = labels.get(k)
            verdict = ""
            if lb and not lb.get("uncertain"):
                if lb.get("is_music") is False:
                    verdict = "correct" if pr.get("is_music") is False else "wrong"
                elif not pr.get("title"):
                    verdict = "wrong" if pr.get("is_music") is False else "abstain"
                else:
                    ok_t = fold(pr["title"]) == fold(lb.get("title"))
                    ok_a = lb.get("artist") is None or fold(pr.get("artist")) == fold(lb["artist"])
                    verdict = "correct" if ok_t and ok_a else "wrong"
                src = pr.get("source", "?").split(":")[0]
                stats.setdefault(src, {"correct": 0, "wrong": 0, "abstain": 0})[verdict] += 1
            w.writerow([k, pr.get("source"), pr.get("is_music"), pr.get("artist"), pr.get("title"),
                        pr.get("confidence"), (lb or {}).get("artist"), (lb or {}).get("title"),
                        (lb or {}).get("is_music"), verdict])
    print(f"wrote {out}")
    for src, s in (stats.items() if labels else []):
        answered = s["correct"] + s["wrong"]
        prec = s["correct"] / answered if answered else 0
        print(f"{src:6} correct={s['correct']} wrong={s['wrong']} abstain={s['abstain']} precision={prec:.1%}")


if __name__ == "__main__":
    main()
