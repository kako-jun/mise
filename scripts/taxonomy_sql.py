#!/usr/bin/env python3
"""カテゴリの木の入れ替え migration を生成する(Issue #114 第1段)。正本は data/categories.csv(新しい木)と data/category-map.csv(旧→新の対応表)で、SQL は派生物。
手で編集しない。ネットワーク・LLM は使わない。

  uv run python3 scripts/taxonomy_sql.py > migrations/0021_taxonomy.sql

**0021 専用の一回限りのツール**。0021 は適用したら凍結する(書き換えない)。data/categories.csv はこの後も育つので、再生成すると 0021 と食い違う。
木を育てる(葉に子を足す・名前を直す)ときは、新しい migration を手で書き、data/categories.csv も同じ変更にする(tests/test_taxonomy.py が両者の一致を検査する。
手順と成長則は docs/design/data-model.md)。data/category-map.csv(旧 59 → 新)は LLM 側の旧 id 互換(scripts/llm/judge.py)にも使う凍結データ。
"""
import csv, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from public_bodies_sql import load, listed, norm, norm_sql, q  # noqa: E402

TREE = ROOT / 'data' / 'categories.csv'
MAP = ROOT / 'data' / 'category-map.csv'
LEDGER = ROOT / 'data' / 'public-bodies' / 'ledger.csv'
OLD_TREE = ROOT / 'migrations' / '0001_initial.sql'
OLD_GOV = 'government-government-agencies'
# 官公庁台帳の type(= 0015 が各サイトに付けたタグ label_ja) -> 行政・公的機関の葉
GOV_LEAF = {'国の機関': 'national-agencies', '独立行政法人': 'independent-agencies', '都道府県': 'prefectures', '市': 'cities',
            '特別区': 'special-wards', '町': 'towns', '村': 'villages'}


def read_tree():
    with open(TREE, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def read_map():
    with open(MAP, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def old_ids():
    return re.findall(r"INSERT INTO categories VALUES \('([^']+)'", OLD_TREE.read_text(encoding='utf-8'))


def ordered(tree):
    """親が先になる並び(深さ順。同じ深さでは CSV の順)。"""
    by = {r['id']: r for r in tree}
    def depth(r):
        d = 0
        while r['parent_id']:
            r = by[r['parent_id']]; d += 1
        return d
    return sorted(tree, key=depth)  # sorted は安定


def in_list(ids):
    return ','.join(q(i) for i in ids)


def generate():
    tree, mp = read_tree(), read_map()
    new = {r['id'] for r in tree}
    old = old_ids()
    old_only = [i for i in old if i not in new]
    assert {r['old_id'] for r in mp} == set(old), '対応表が旧カテゴリ全件を覆っていない'
    for r in mp:
        assert r['mode'] in ('keep', 'map', 'fallback', 'drop', 'rule'), r
        if r['mode'] in ('keep', 'map', 'fallback'): assert r['new_id'] in new, r
    leaves = list(GOV_LEAF.values())
    assert all(l in new for l in leaves)
    out = [
        '-- カテゴリの木の入れ替え(Issue #114 第1段)。data/categories.csv と data/category-map.csv から scripts/taxonomy_sql.py が生成した派生物。手で編集しない。適用後は凍結(書き換えない)。',
        f'-- 旧 {len(old)} カテゴリ -> 新 {len(tree)} カテゴリ(主題の木のみ。官公庁は「行政・公的機関 > 種別 7 葉」へ決定論で移す。範囲の都道府県化は第2段)。',
        '-- 順序: (1) 新しい木を入れる (2) 旧→新の対応表で site_categories を移す (3) 官公庁を種別の葉へ (4) 退避先(fallback)を足す (5) 取りこぼしがあれば',
        '--       CHECK で失敗させてこの migration ごと取り消す (6) 旧の行と旧カテゴリを消す。サイト(sites)・タグ・検索用テキストには触れない。',
        '',
        '-- (1) 新しい木。同じ id を引き継ぐトップ(arts business reference education science government health society)は名前・並びを上書きする。',
    ]
    for r in ordered(tree):
        out.append(f"INSERT INTO categories (id,parent_id,name_ja,name_en,sort_order) VALUES ({q(r['id'])},{q(r['parent_id']) if r['parent_id'] else 'NULL'},{q(r['name_ja'])},{q(r['name_en'])},{r['sort_order']}) "
                   "ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id,name_ja=excluded.name_ja,name_en=excluded.name_en,sort_order=excluded.sort_order;")
    out += ['', '-- (2) 旧→新の対応表(mode=map)。複数の旧カテゴリが同じ新カテゴリに入ってもよい(主キーで重複は無視される)。']
    for r in mp:
        if r['mode'] == 'map':
            out.append(f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT site_id,{q(r['new_id'])} FROM site_categories WHERE category_id={q(r['old_id'])};")
    types = in_list(GOV_LEAF)
    case = ' '.join(f"WHEN {q(t)} THEN {q(l)}" for t, l in GOV_LEAF.items())
    out += ['', f'-- (3) 官公庁({OLD_GOV})。種別は 0015 が各サイトに付けた type タグ(label_ja = 台帳の type)から振る。',
            "--     id が pb- で始まるサイトだけが対象(手動掲載の同一サイトは次の URL 照合で拾う)。",
            f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT sc.site_id, CASE t.label_ja {case} END FROM site_categories sc JOIN site_tags t ON t.site_id=sc.site_id AND t.label_ja IN ({types}) "
            f"WHERE sc.category_id={q(OLD_GOV)} AND sc.site_id LIKE 'pb-%';"]
    nat = [r for c, r in sorted(listed(load(LEDGER), False).items()) if r['type'] in ('国の機関', '独立行政法人')]
    out += ['', '--     0015 は URL が既存の手動掲載サイトと重複する団体を新規に入れず、その既存サイト(国立国会図書館 = sample-6)にだけ旧カテゴリを付けた。',
            '--     pb- でないのでタグ経由では拾えない。国の機関・独立行政法人は台帳の URL で照合して葉を付ける(まだ葉の無いサイトだけ)。']
    for typ in ('国の機関', '独立行政法人'):
        keys = [norm(r['url']) for r in nat if r['type'] == typ]
        for i in range(0, len(keys), 150):
            out.append(f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT s.id,{q(GOV_LEAF[typ])} FROM sites s JOIN site_categories sc ON sc.site_id=s.id AND sc.category_id={q(OLD_GOV)} "
                       f"WHERE {norm_sql('s.url')} IN ({in_list(keys[i:i + 150])}) AND NOT EXISTS (SELECT 1 FROM site_categories x WHERE x.site_id=s.id AND x.category_id IN ({in_list(leaves)}));")
    out += ['', "--     それでも葉の無い官公庁カテゴリのサイトは、行政・公的機関(トップ)に置く(想定は 0 件。件数は PR の検証で確認する)。",
            f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT sc.site_id,'government' FROM site_categories sc WHERE sc.category_id={q(OLD_GOV)} "
            f"AND NOT EXISTS (SELECT 1 FROM site_categories x WHERE x.site_id=sc.site_id AND x.category_id IN ({in_list(leaves)}));"]
    renew = ','.join(q(i) for i in old_only)
    out += ['', '-- (4) 退避先(mode=fallback)。主題でなく運営主体・地域だった旧カテゴリ。他に新しい木のカテゴリを持たないサイトにだけ退避先を付ける(再判定で主題へ移す前提)。']
    for r in mp:
        if r['mode'] == 'fallback':
            out.append(f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT sc.site_id,{q(r['new_id'])} FROM site_categories sc WHERE sc.category_id={q(r['old_id'])} "
                       f"AND NOT EXISTS (SELECT 1 FROM site_categories x WHERE x.site_id=sc.site_id AND x.category_id NOT IN ({renew}));")
    out += ['', '-- (5) 検査。旧カテゴリしか持たないサイトが 1 件でも残っていれば CHECK 違反でこの migration 全体が失敗する(旧カテゴリを消す前に止める)。',
            'CREATE TABLE _taxonomy_check (orphans INTEGER NOT NULL CHECK (orphans = 0));',
            f"INSERT INTO _taxonomy_check SELECT count(*) FROM (SELECT site_id FROM site_categories GROUP BY site_id HAVING sum(category_id NOT IN ({renew})) = 0);",
            'DROP TABLE _taxonomy_check;',
            '', '-- (6) 旧カテゴリの整理。site_categories(FK) -> categories の順。同じ id を引き継いだトップは残す。',
            f"DELETE FROM site_categories WHERE category_id IN ({renew});",
            f"DELETE FROM categories WHERE id IN ({renew});"]
    return '\n'.join(out) + '\n'


if __name__ == '__main__':
    sys.stdout.write(generate())
