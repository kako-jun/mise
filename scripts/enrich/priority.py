"""優先順(機械的): 掲載数が FLOOR 未満の葉カテゴリを埋める行を先に処理する。人気・規模・トラフィックは一切使わない。

rank 0 = url_candidates.hint_leaf が「不足(deficit>0)の葉」。不足の大きい葉が先、同数なら identity_key 順。
rank 1 = ヒントなし(または葉でない値)。identity_key 順。
rank 2 = ヒントの葉が既に FLOOR 以上。identity_key 順。(落とさない。後回しにするだけ)
公平枠: FAIRNESS_EVERY 回に 1 回の選択バッチは優先せず identity_key 順だけで選ぶ(rank 2 が永久に止まらない)。
葉 id は categories 表の信頼できる id(正規表現で検証)で、SQL に定数として埋め込む(束縛変数 0。D1 の 100 個制限に触れない)。
不足 = max(0, FLOOR - 掲載数(site_categories) - 処理中(url_progress が fetched/reviewing/approved/queued の行))。
"""
import collections
import csv
import re
from pathlib import Path

FLOOR = 100                # 葉 1 つあたりの掲載数の目標
FAIRNESS_EVERY = 5         # この回数に 1 回は identity_key の機械順だけで選ぶ
INFLIGHT_STATES = ('fetched', 'reviewing', 'approved', 'queued')
LEAF_ID = re.compile(r'^[a-z0-9][a-z0-9-]*$')
MODES = ('leaf', 'none')
# 行の葉 = 判定済みなら categories の先頭、無ければ hint_leaf。処理中の数え方(leaf_deficits)と rank の両方でこの同じ式を使う
LEAF_EXPR = ("COALESCE(CASE WHEN json_valid(c.categories) AND json_type(c.categories)='array' THEN json_extract(c.categories,'$[0]') END, "
             "c.hint_leaf)")


def _safe(ids):
    for i in ids:
        if not isinstance(i, str) or not LEAF_ID.match(i):
            raise ValueError(f'invalid category id: {i!r}')
    return ids


CATEGORIES_CSV = Path(__file__).resolve().parents[2] / 'data' / 'categories.csv'


def rule_leaf_ids(path=CATEGORIES_CSV):
    """assign=rule の葉(国の機関・都道府県・市・町・村など。発見ではなく規則で埋める)。D1 の categories に assign 列は無いので CSV を正本にする。
    葉の不足の定義(優先順・葉狙いの発見・未対応レポート)は全部この集合を除く。"""
    with open(path, encoding='utf-8', newline='') as f:
        return {r['id'] for r in csv.DictReader(f) if r['assign'] == 'rule'}


def leaf_ids(client):
    rows = client.query("SELECT id FROM categories WHERE id NOT IN (SELECT parent_id FROM categories WHERE parent_id IS NOT NULL) ORDER BY id")
    rule = rule_leaf_ids()
    return _safe([r['id'] for r in rows if r['id'] not in rule])


def leaf_deficits(client):
    """-> {葉 id: 不足数(0 以上)}。全ての葉を含む(不足 0 も含める)。集計 SQL は 3 本。"""
    leaves = leaf_ids(client)
    published = {r['category_id']: r['n'] for r in client.query("SELECT category_id, COUNT(*) AS n FROM site_categories GROUP BY category_id")}
    states = ','.join(f"'{s}'" for s in INFLIGHT_STATES)
    inflight = collections.Counter()
    for r in client.query(
            f"SELECT {LEAF_EXPR} AS leaf, COUNT(*) AS n FROM url_progress p JOIN url_candidates c ON c.identity_key=p.identity_key "
            f"WHERE p.state IN ({states}) GROUP BY leaf"):
        if r['leaf']:
            inflight[r['leaf']] += r['n']
    return {leaf: max(0, FLOOR - int(published.get(leaf, 0)) - inflight[leaf]) for leaf in leaves}


def rank_sql(deficits, col=LEAF_EXPR):
    """-> (rank 式, 不足式)。ORDER BY <rank>, <不足> DESC, identity_key に使う。col は SQL の列(固定の文字列)。"""
    _safe(deficits)
    need = sorted((k for k, v in deficits.items() if v > 0), key=lambda k: (-deficits[k], k))
    full = sorted(k for k, v in deficits.items() if v <= 0)
    q = lambda ids: ','.join(f"'{i}'" for i in ids)
    parts = []
    if need:
        parts.append(f'WHEN {col} IN ({q(need)}) THEN 0')
    if full:
        parts.append(f'WHEN {col} IN ({q(full)}) THEN 2')
    rank = f"(CASE {' '.join(parts)} ELSE 1 END)" if parts else '1'
    dsql = '(CASE ' + ' '.join(f"WHEN {col}='{k}' THEN {deficits[k]}" for k in need) + ' ELSE 0 END)' if need else '0'
    return rank, dsql


class Selector:
    """段ごとに 1 つ。選択のたびに order() を呼ぶ。mode='none' は従来どおり identity_key 順。"""

    def __init__(self, mode='leaf', every=FAIRNESS_EVERY):
        if mode not in MODES:
            raise ValueError(f'priority mode must be one of {MODES}')
        self.mode, self.every, self.batches, self.deficits, self.fair = mode, every, 0, {}, False

    def order(self, client, key_col, hint_col=LEAF_EXPR):
        """-> (ORDER BY の中身, SELECT に足す列)。公平枠・mode none は rank を付けない。"""
        self.batches += 1
        self.fair = self.mode == 'none' or self.batches % self.every == 0
        if self.mode == 'none':
            return key_col, ''
        self.deficits = leaf_deficits(client)
        rank, dsql = rank_sql(self.deficits, hint_col)
        if self.fair:
            return key_col, f', {rank} AS prio_rank'
        return f'prio_rank, {dsql} DESC, {key_col}', f', {rank} AS prio_rank'

    def report(self, rows, log, stage):
        if self.mode == 'none':
            return
        top = sorted(((k, v) for k, v in self.deficits.items() if v > 0), key=lambda kv: (-kv[1], kv[0]))[:5]
        n = collections.Counter(r.get('prio_rank') for r in rows)
        log(f'{stage} priority: top deficit leaves={top or "none"}; selected rank0/1/2={n[0]}/{n[1]}/{n[2]}'
            + (' (fairness batch: plain identity_key order)' if self.fair else ''))
