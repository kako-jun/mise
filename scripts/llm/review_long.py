#!/usr/bin/env python3
"""Markdown review table for long descriptions: output vs. the source text it must be grounded in.

Usage: review_long.py <candidates.jsonl used as input> <results.json> [<results2.json> ...]
Automatic flags (a human still reads the table):
  numbers: digit strings in the long description that do not occur in the source text
  words:   katakana / latin words (3+ chars) in the Japanese long description absent from the source text
  kanji:   kanji runs (3-6 chars) absent from the source text (new proper nouns show up here)
  adult:   the model's adult attribute (descriptions of such sites must stay neutral)
"""
import json
import pathlib
import re
import sys
import unicodedata

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import judge  # noqa: E402


def source_text(c):
    return judge.render_candidate(c)


def flags(o, src):
    out = []
    src = unicodedata.normalize("NFKC", src)  # full-width digits / latin in the source
    ja = unicodedata.normalize("NFKC", o["long_description_ja"] or "")
    nums = sorted({n for n in re.findall(r"\d+", ja) if n not in src})
    if nums:
        out.append("numbers not in source: " + ", ".join(nums))
    words = sorted({w for w in re.findall(r"[ァ-ヴー]{3,}|[A-Za-z]{3,}", ja) if w.lower() not in src.lower()})
    if words:
        out.append("words not in source: " + ", ".join(words[:8]))
    flat = re.sub(r"\s+", "", src)
    kanji = sorted({w for w in re.findall(r"[一-鿿]{3,6}", ja) if w not in flat})
    if kanji:
        out.append("kanji words not in source (check for paraphrase vs. new fact): " + ", ".join(kanji[:10]))
    return out


def load_results(path):
    """judge.py results (a list) or pipeline.py results (a dict with "results"; model column = file name)."""
    d = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if isinstance(d, dict):
        name = path.rsplit("/", 1)[-1].removesuffix(".json")
        return {r["id"]: dict(r, model=name) for r in d["results"]}
    return {r["id"]: r for r in d}


def main(inp, *results):
    cands = {json.loads(l)["id"]: json.loads(l) for l in pathlib.Path(inp).read_text(encoding="utf-8").splitlines() if l.strip()}
    runs = [(p, load_results(p)) for p in results]
    print("| site | model | long_description_ja | evidence quotes (verbatim, checked by validator) | auto flags |")
    print("|---|---|---|---|---|")
    for sid, c in cands.items():
        src = source_text(c)
        for p, rs in runs:
            r = rs.get(sid)
            if not r or not r.get("output"):
                print(f"| {sid} | ? | INVALID | | |")
                continue
            o = r["output"]
            model = r["model"].replace("claude-", "").replace("-5-5", "")
            ja = (o["long_description_ja"] or "(null)").replace("|", "\\|")
            ev = "<br>".join(q.replace("|", "\\|") for q in o["long_description_evidence"]) or "-"
            fl = "; ".join(flags(o, src)) or "-"
            print(f"| {sid} | {model} | {ja} | {ev} | adult={o['adult']['value']}; {fl} |")


if __name__ == "__main__":
    main(*sys.argv[1:])
