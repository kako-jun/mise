#!/usr/bin/env python3
"""名簿の差分追従（骨組み）。2 つの台帳 CSV（旧/新）を比べ、変更を種類別に出す。台帳も公開も自動では書き換えない。

検出する種類:
  新設        新しい団体コードが現れた
  廃止/合併   status が active から merged/abolished に変わった（後継コードを併記）
  名称変更    同じ団体コードで name_ja が変わった
  URL変更     同じ団体コードで host が変わった（旧URL→新URL。移転は人が確認）
  消失        旧台帳にあった行が新台帳に無い（削除せず状態を変える方針なので異常。要調査）
  確認状態    ledger（掲載可）と review（要確認）の間の移動 ※ --old-review/--new-review を渡した場合
使い方:
  1) 前回の台帳を退避:   cp data/public-bodies/ledger.csv /tmp/ledger-old.csv
  2) 再取得と再生成:     uv run python3 scripts/public_bodies_fetch.py && uv run --with openpyxl python3 scripts/public_bodies_build.py
  3) 差分:               uv run python3 scripts/public_bodies_diff.py /tmp/ledger-old.csv data/public-bodies/ledger.csv
出力は CSV（change,code,name_ja,old,new,detail）。終了コードは差分があれば 1。
"""
import csv, sys

def load(path):
    return {r['code']: r for r in csv.DictReader(open(path, encoding='utf-8'))}

def diff(old, new):
    out = []
    for c, n in sorted(new.items()):
        o = old.get(c)
        if o is None:
            out.append(('新設', c, n['name_ja'], '', n['status'], n['url'])); continue
        if o['status'] != n['status']:
            kind = '廃止/合併' if n['status'] in ('merged', 'abolished') else '状態変更'
            out.append((kind, c, n['name_ja'], o['status'], n['status'], f"施行日={n['status_date']} 後継={n['successor_code']} {n['note']}"))
        if o['name_ja'] != n['name_ja']:
            out.append(('名称変更', c, n['name_ja'], o['name_ja'], n['name_ja'], ''))
        if o['host'] != n['host'] and (o['host'] or n['host']):
            out.append(('URL変更', c, n['name_ja'], o['url'], n['url'], f"host {o['host']} -> {n['host']}"))
    for c, o in sorted(old.items()):
        if c not in new: out.append(('消失', c, o['name_ja'], o['status'], '', '台帳から行が消えた。要調査'))
    return out

def main():
    if len(sys.argv) != 3: raise SystemExit(__doc__)
    rows = diff(load(sys.argv[1]), load(sys.argv[2]))
    w = csv.writer(sys.stdout, lineterminator='\n'); w.writerow(['change', 'code', 'name_ja', 'old', 'new', 'detail'])
    w.writerows(rows)
    sys.exit(1 if rows else 0)

if __name__ == '__main__': main()
