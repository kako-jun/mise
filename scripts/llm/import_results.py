#!/usr/bin/env python3
"""Import judge.py / pipeline.py output JSON into the frontier ledger (table llm_judgement). Issue #48.

  uv run python3 scripts/llm/import_results.py RESULTS.json --input CANDIDATES.jsonl [--db var/frontier.sqlite]

RESULTS.json is what pipeline.py (an object with "results") or judge.py (a list) wrote. --input is the JSONL that was
given to the judge (scripts/llm/export_candidates.py output): the candidate text is needed to fingerprint the input and
to re-validate the output (including verbatim quotes) before anything enters the ledger. No network, no API.

Rules (details: docs/collection/llm-judge.md):
- A result whose id does not match exactly one ledger key, whose candidate is not in --input, whose candidate URL differs
  from the ledger row, or whose output fails judge.validate_output is rejected (counted, never written).
- The candidate text is rendered with the body limit the judge ran with (pipeline: args.body_limit, judge.py: the
  record's body_limit; judge.BODY_LIMIT when an old output has none), so the hash covers what the LLM actually saw.
- Idempotent: the input hash = sha256(origin, model, rendered candidate text). The same hash is skipped. A new hash replaces
  the row and puts review_state back to pending (verdict reject: rejected), whatever it was before.
- verdict reject is stored with review_state=rejected; deferred / pass / suspect start as pending. frontier.state is
  never changed.
- A results file older than the stored judgement of that host (file mtime = judged_at) is rejected, so a stale file
  cannot overwrite a newer judgement and put an approved row back to pending. --allow-older overrides.
- adult=yes: no long description, no quotes (adult / long-description / location evidence) are stored.
Exit code: 0 all fine, 2 when some results were rejected, 1 on usage errors.
"""
import argparse
import datetime
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts", "pipeline"))
import judge  # noqa: E402
import frontier  # noqa: E402
from export_candidates import cand_id  # noqa: E402


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    raise SystemExit(f"{path}:{n}: not valid JSON")
    return out


def load_results(path):
    """-> (origin, [(id, output|None, model, error, body_limit)]). Raises SystemExit on a shape we do not know."""
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError:
            raise SystemExit(f"{path}: not valid JSON")
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        args = data.get("args") or {}
        base = args.get("model")
        if not isinstance(base, str) or not base:
            raise SystemExit(f"{path}: pipeline output without args.model")
        desc = args.get("describe_model")
        items = []
        for r in data["results"]:
            if not isinstance(r, dict) or "id" not in r:
                raise SystemExit(f"{path}: result without id")
            out = r.get("output")
            described = out is not None and out is not r.get("triage") and out != r.get("triage")
            model = f"{base}+{desc}" if described and desc and desc != base else base
            items.append((r["id"], out, model, r.get("error"), args.get("body_limit") or judge.BODY_LIMIT))
        return "pipeline", items
    if isinstance(data, list):
        items = []
        for r in data:
            if not isinstance(r, dict) or "id" not in r or not isinstance(r.get("model"), str):
                raise SystemExit(f"{path}: judge result without id / model")
            bl = r.get("body_limit")
            items.append((r["id"], r.get("output"), r["model"], r.get("error"),
                          bl if isinstance(bl, int) and not isinstance(bl, bool) and bl > 0 else judge.BODY_LIMIT))
        return "judge", items
    raise SystemExit(f"{path}: neither a pipeline.py output (object with results) nor a judge.py output (list)")


def input_hash(origin, model, text):
    blob = json.dumps({"origin": origin, "model": model, "text": text}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def row_values(origin, model, out, judged_at, imported_at, h):
    """The llm_judgement columns for one validated output (full judge.py shape)."""
    adult = out["adult"]["value"]
    hide = adult == "yes"  # same policy as the pipeline: do not keep body text of adult sites
    loc = out["location"]
    verdict = out["spam"]["verdict"]
    return {
        "input_hash": h, "origin": origin, "model": model, "judged_at": judged_at, "imported_at": imported_at,
        "spam_verdict": verdict, "spam_kind": out["spam"]["kind"], "spam_reason": out["spam"]["reason"],
        "description_ja": out["description_ja"], "description_en": out["description_en"],
        "long_description_ja": None if hide else out["long_description_ja"],
        "long_description_en": None if hide else out["long_description_en"],
        "long_description_evidence": None if hide else json.dumps(out["long_description_evidence"], ensure_ascii=False),
        "categories": json.dumps(out["categories"]), "tags": json.dumps(out["tags"], ensure_ascii=False),
        "adult": adult, "site_kind": out["site_kind"], "site_kind_reason": out["site_kind_reason"],
        "site_scope": out["site_scope"], "confidence": out["confidence"],
        "country_code": loc["country_code"], "region_code": loc["region_code"], "notes": out["notes"],
        "review_state": "rejected" if verdict == "reject" else "pending",
        "reviewed_at": imported_at if verdict == "reject" else None,
    }


def import_results(db, results_path, input_path, allow_older=False):
    """Returns (counts, problems). Writes inside one transaction."""
    origin, items = load_results(results_path)
    cands = {c["id"]: c for c in read_jsonl(input_path)}
    valid_ids = set(judge.category_ids(judge.load_categories()))
    legacy = judge.load_legacy_map()
    by_id = {}
    for r in db.execute("SELECT key, url, final_url FROM frontier"):
        by_id.setdefault(cand_id(r["key"]), []).append(r)
    judged_at = datetime.datetime.fromtimestamp(os.path.getmtime(results_path), datetime.timezone.utc).isoformat(timespec="seconds")
    imported_at = frontier.now()
    counts = {"imported": 0, "updated": 0, "unchanged": 0, "rejected": 0, "no_output": 0}
    problems = []

    def reject(cid, why):
        counts["rejected"] += 1
        problems.append(f"{cid}: {why}")

    for cid, out, model, err, body_limit in items:
        if out is None:
            counts["no_output"] += 1
            continue
        rows = by_id.get(cid, [])
        if len(rows) != 1:
            reject(cid, "no matching host in the ledger" if not rows else f"ambiguous id (matches {len(rows)} ledger keys)")
            continue
        row = rows[0]
        cand = cands.get(cid)
        if cand is None:
            reject(cid, "candidate not in --input")
            continue
        if cand.get("url") != (row["final_url"] or row["url"]):
            reject(cid, "candidate url differs from the ledger row (stale input)")
            continue
        text = judge.render_candidate(cand, body_limit)
        # Old ids (pre-#114) are translated and the set is put in the canonical form (#137: no ancestor next to its descendant;
        # duplicates are merged silently). Unknown ids stay and are rejected by validate_output as before. Because of this step
        # the result always is canonical: there is no separate "not canonical" rejection.
        if isinstance(out.get("categories"), list):
            out = dict(out, categories=judge.canonical_categories(out["categories"], valid_ids, legacy))
        errs = judge.validate_output(out, valid_ids, text)
        if errs:
            reject(cid, "invalid output: " + "; ".join(errs))
            continue
        h = input_hash(origin, model, text)
        old = db.execute("SELECT input_hash, judged_at FROM llm_judgement WHERE key=?", (row["key"],)).fetchone()
        if old and old["input_hash"] == h:
            counts["unchanged"] += 1
            continue
        if old and not allow_older and (old["judged_at"] or "") > judged_at:
            reject(cid, f"results file ({judged_at}) is older than the stored judgement ({old['judged_at']}); --allow-older to overwrite")
            continue
        vals = row_values(origin, model, out, judged_at, imported_at, h)
        cols = ["key"] + list(vals)
        db.execute(f"INSERT OR REPLACE INTO llm_judgement ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                   [row["key"]] + list(vals.values()))
        counts["updated" if old else "imported"] += 1
    db.commit()
    return counts, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results")
    ap.add_argument("--input", required=True, help="the candidates JSONL that was given to the judge")
    ap.add_argument("--db", default=None, help="frontier sqlite (default var/frontier.sqlite)")
    ap.add_argument("--allow-older", action="store_true", help="let a results file older than the stored judgement overwrite it")
    a = ap.parse_args(argv)
    db = frontier.connect(a.db)
    try:
        counts, problems = import_results(db, a.results, a.input, a.allow_older)
    finally:
        db.close()
    for p in problems:
        print("REJECTED " + p, file=sys.stderr)
    print(", ".join(f"{k}={v}" for k, v in counts.items()), file=sys.stderr)
    return 2 if counts["rejected"] else 0


if __name__ == "__main__":
    sys.exit(main())
