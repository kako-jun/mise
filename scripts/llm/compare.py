#!/usr/bin/env python3
"""Print judge results next to the human-written reference (var/sites-reference.json)."""
import json
import pathlib
import sys

def main(ref_path, *result_paths):
    ref = {r["id"]: r for r in json.loads(pathlib.Path(ref_path).read_text(encoding="utf-8"))}
    runs = [(p, {r["id"]: r for r in json.loads(pathlib.Path(p).read_text(encoding="utf-8"))}) for p in result_paths]
    for sid, h in ref.items():
        print(f"== {sid} {h['name']}  human: cats={h['categories']} country={h['country_code']} region={h['region_code']}")
        print(f"   human ja: {h['description_ja']}\n   human en: {h['description_en']}")
        for p, rs in runs:
            r = rs.get(sid)
            tag = r["model"] if r else "?"
            if not r or not r.get("output"):
                print(f"   [{tag}] INVALID {r.get('error') if r else ''}")
                continue
            o = r["output"]
            print(f"   [{tag}] ${r['cost_usd']:.5f} {r['seconds']:.1f}s spam={o['spam']['verdict']}/{o['spam']['kind']} "
                  f"kind={o['site_kind']} scope={o['site_scope']} adult={o['adult']['value']} conf={o['confidence']} "
                  f"cats={o['categories']} loc={o['location']['country_code']}/{o['location']['region_code']}")
            print(f"      ja: {o['description_ja']}\n      en: {o['description_en']}")
            print(f"      why: {o['spam']['reason']} | tags={o['tags']} | notes={o['notes']}")

if __name__ == "__main__":
    main(*sys.argv[1:])
