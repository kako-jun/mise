#!/usr/bin/env python3
"""台帳(ledger.csv)から D1 投入用 migration を生成する(Issue #37 第2段)。台帳が正本で、SQL は派生物。LLM・ネットワークは使わない。

  # 初回: active の全団体を投入
  uv run python3 scripts/public_bodies_sql.py > migrations/0015_public_bodies.sql
  # 以降の追従: 適用済みの時点の台帳(git show <commit>:data/public-bodies/ledger.csv > /tmp/old.csv)との差分だけを出す
  uv run python3 scripts/public_bodies_sql.py --since /tmp/old.csv > migrations/00NN_public_bodies_update.sql

投入する範囲: status=active の都道府県・市・町・村・特別区・国の機関・独立行政法人。政令市の行政区は市サイトの配下で
出典の裏付けも弱いため既定では投入しない(--include-wards で含める。掲載単位は Issue #37 で要判断)。
適用済みの migration は書き換えない。台帳が変わったら --since で新しい migration を足す。
  新設 -> INSERT / 廃止・合併・要確認へ降格(active でなくなった) -> 掲載から DELETE / 名称・URL の変更 -> UPDATE。
id は台帳の団体コードから決まる(pb-<コード>)。ホスト(www. 除去・小文字)+パスが既に別の掲載サイトにある団体は INSERT しない(重複掲載しない)。
その既存サイトには政府機関カテゴリだけを追加する(カテゴリは多重所属できる)。
"""
import argparse, csv, pathlib, re, sys
from public_bodies_common import clean_url

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'data' / 'public-bodies'
CATEGORY = 'government-government-agencies'
LOCAL = ('都道府県', '市', '町', '村', '特別区')
NATIONAL = ('国の機関', '独立行政法人')
TYPE_EN = {'都道府県': 'Prefecture', '市': 'City', '町': 'Town', '村': 'Village', '特別区': 'Special ward', '行政区': 'Ward',
           '国の機関': 'National government', '独立行政法人': 'Incorporated administrative agency'}

def q(s):
    return "'" + s.replace("'", "''") + "'"

def norm(u):
    """重複判定キー: 小文字化・scheme 除去・'www.' 除去・index系/末尾スラッシュ除去。SQL の NORM_SQL と同じ変換にする。"""
    return clean_url(u).lower().replace('https://', '').replace('http://', '').replace('www.', '').rstrip('/')

def norm_sql(col):
    return f"rtrim(replace(replace(replace(lower({col}),'https://',''),'http://',''),'www.',''),'/')"

def site_id(code):
    return 'pb-' + code.replace(':', '-')

def load(path):
    return list(csv.DictReader(open(path, encoding='utf-8')))

def listed(rows, include_wards):
    types = LOCAL + NATIONAL + (('行政区',) if include_wards else ())
    return {r['code']: r for r in rows if r['status'] == 'active' and r['type'] in types}

def pref_en(rows):
    """都道府県名 -> 英語名(台帳の都道府県行から)。"""
    return {r['name_ja']: r['name_en'] for r in rows if r['type'] == '都道府県' and r['name_en']}

def describe(r, pen):
    """紹介文(定型。ページの内容は読んでいない)。(日本語, 英語 or None)"""
    if r['type'] in NATIONAL:
        kind = '国の機関' if r['type'] == '国の機関' else '独立行政法人'
        return f'{kind}「{r["name_ja"]}」の公式サイト。', None
    place = r['name_ja'] if r['type'] == '都道府県' else r['prefecture'] + r['name_ja']
    ja = f'{place}の公式サイト。'
    en = None
    if r['name_en']:
        en = f'Official website of {r["name_en"]}' + ('' if r['type'] == '都道府県' or not pen.get(r['prefecture']) else f', {pen[r["prefecture"]]}') + ', Japan.'
    return ja, en

def tags(r, pen):
    out = [(r['type'], TYPE_EN[r['type']])]
    if r['prefecture'] and r['type'] != '都道府県': out.append((r['prefecture'], pen.get(r['prefecture'])))
    return out

def region(r):
    return 'JP-' + r['code'][:2] if re.fullmatch(r'\d{6}', r['code']) and 1 <= int(r['code'][:2]) <= 47 else None

def confirmed_at(r):
    """registered_at: 台帳の sources にある出典確認日の最新(YYYY-MM-DD)。投入時刻ではなく、出典で確認した日。"""
    days = re.findall(r'@(\d{4}-\d{2}-\d{2})', r['sources'])
    return (max(days) if days else '1970-01-01') + 'T00:00:00Z'

def insert_sql(r, pen):
    ja, en = describe(r, pen)
    reg = region(r)
    cols = "id,url,name,description_ja,description_en,country_code,registered_at,bulk_import" + (",region_code" if reg else "")
    vals = f"{q(site_id(r['code']))},{q(r['url'])},{q(r['name_ja'])},{q(ja)},{q(en) if en else 'NULL'},'JP',{q(confirmed_at(r))},1" + (f",{q(reg)}" if reg else "")
    # URL が既にあるサイトと重複する場合は何も入れない(後続の category/tag は id で結合するので空振りになる)
    return f"INSERT INTO sites ({cols}) SELECT {vals} WHERE NOT EXISTS (SELECT 1 FROM sites WHERE {norm_sql('url')}={q(norm(r['url']))});"

def child_sql(rs, pen):
    """カテゴリとタグ。投入された id にだけ付くよう sites と結合する。"""
    out = []
    # カテゴリは URL の正規化キーで付ける: 新規に入れた行と、すでに別 id で掲載されている同じサイト(例: 国立国会図書館)の両方に付く
    for i in range(0, len(rs), 150):
        keys = ','.join(q(norm(r['url'])) for r in rs[i:i + 150])
        out.append(f"INSERT OR IGNORE INTO site_categories (site_id,category_id) SELECT id,{q(CATEGORY)} FROM sites WHERE {norm_sql('url')} IN ({keys});")
    for i in range(0, len(rs), 100):
        vals = ','.join(f"({q(site_id(r['code']))},{q(ja)},{q(en) if en else 'NULL'})" for r in rs[i:i + 100] for ja, en in tags(r, pen))
        out.append(f"INSERT OR IGNORE INTO site_tags (site_id,label_ja,label_en) SELECT v.a,v.b,v.c FROM (SELECT column1 AS a,column2 AS b,column3 AS c FROM (VALUES {vals})) v WHERE v.a IN (SELECT id FROM sites);")
    return out

def generate(rows, old_rows=None, include_wards=False):
    pen = pref_en(rows)
    new = listed(rows, include_wards)
    old = listed(old_rows, include_wards) if old_rows is not None else {}
    added = [new[c] for c in sorted(new) if c not in old]
    removed = [old[c] for c in sorted(old) if c not in new]
    changed = [new[c] for c in sorted(new) if c in old and (new[c]['name_ja'], new[c]['url']) != (old[c]['name_ja'], old[c]['url'])]
    out = ['-- 官公庁・自治体の公式サイト(Issue #37)。data/public-bodies/ledger.csv から scripts/public_bodies_sql.py が生成した派生物。手で編集しない。',
           '-- 根拠は台帳の sources 列(独立した2出典以上の一致)。紹介文は定型で、トップページの内容は読んでいない。last_crawled_at・source_updated_at は未設定。',
           '-- registered_at は出典の確認日。bulk_import=1 は一括投入の印で、直近登録の集計・ACTIVITY・NEW タグ・既定の並びの先頭から外す(ALTER は初回のみ)。',
           f'-- 新設/追加 {len(added)} 件' + (f' / 掲載から外す {len(removed)} 件 / 名称・URL変更 {len(changed)} 件' if old_rows is not None else '') + '。']
    if old_rows is None: out.append("ALTER TABLE sites ADD COLUMN bulk_import INTEGER NOT NULL DEFAULT 0 CHECK (bulk_import IN (0, 1));")
    for r in added: out.append(insert_sql(r, pen))
    if added: out += child_sql(added, pen)
    for r in changed:
        ja, en = describe(r, pen)
        out.append(f"UPDATE sites SET url={q(r['url'])},name={q(r['name_ja'])},description_ja={q(ja)},description_en={q(en) if en else 'NULL'} WHERE id={q(site_id(r['code']))};")
    for r in removed:
        i = q(site_id(r['code']))
        out += [f"DELETE FROM site_tags WHERE site_id={i};", f"DELETE FROM site_categories WHERE site_id={i};", f"DELETE FROM sites WHERE id={i};"]
    return '\n'.join(out) + '\n'

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ledger', default=str(ROOT / 'ledger.csv'))
    ap.add_argument('--since', help='適用済みの時点の台帳 CSV。指定すると差分だけを出す')
    ap.add_argument('--include-wards', action='store_true', help='政令市の行政区も含める')
    a = ap.parse_args()
    sys.stdout.write(generate(load(a.ledger), load(a.since) if a.since else None, a.include_wards))

if __name__ == '__main__': main()
