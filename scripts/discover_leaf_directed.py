"""葉(カテゴリの末端)に狙いを定めた URL 発見(#181, 2026-11-25 方針)。scripts/discover_online.py の run() から呼ばれる。

目的: リンクを盲目的にたどるのをやめ、公開サイトが FLOOR(100)件に満たない葉へ向けて URL を探し、
見つけた URL ごとに「どの葉を狙って見つけたか」を url_candidates.hint_leaf に残す。
原則: どんなに小さな個人サイトも対等に扱う。人気・規模で選別も並べ替えもしない。除外は入口(SNS・ポータル等)と安全上の拒否だけ。
順序は機械的: 葉は不足(FLOOR - 公開数)の大きい順、同数は葉 id の文字列順。リンク元は葉ごとに sites.id の昇順。

ローカルに状態を持たない(ファイル・キャッシュ・SQLite を作らない)。続きの位置は D1 の discovery_cursors:
  wikidata-leaves / <leaf>:<rel>:<qid>   = 最後に取った項目 ID('Q123')または 'done'
  links-leaf      / <leaf>               = その葉で最後に処理し終えた sites.id
"""
import collections
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'enrich'))
import discover_links_cc as dlc
import discover_wikidata as dw
import priority   # 葉の定義(assign=rule を除く)を優先順と共通にする

FLOOR = 100                 # 公開サイトがこれに満たない葉が対象
COVER_FACTOR = 3            # 公開 + 取り込み中(hint つき)が FLOOR の何倍に達したらその葉を止めるか
MAX_PARAMS = 90             # D1 は 1 文 100 個まで
TAG_ROWS = 15               # hint 付き upsert の 1 文の行数(5 パラメータ/行)
BATCH_STATEMENTS = 5
LEAF_PAGE = 200
MIN_PAGE = 50               # 残り枠が小さくても 1 ページはこの件数まで取る(既知の行が多いと枠が埋まらないため)
IN_PIPELINE = ('discovered', 'queued', 'fetched', 'candidate', 'reviewing', 'approved')   # hint 付きで取り込み中とみなす url_progress.state
ENTITY = 'http://www.wikidata.org/entity/'
_QID = re.compile(r'^Q[1-9]\d{0,9}$')
SUGGESTION = '公式サイトより、個人サイトの リンク集・ウェブリング・その分野の公式の一覧ページ から探す(URL は人が確認する。推測で作らない)'


def chunks(items, n):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


# ------------------------------------------------------------------ 不足の計算
def leaf_state(client, floor=FLOOR):
    """葉(子を持たない assign=llm のカテゴリ。priority.leaf_ids と同じ定義)ごとの {leaf: {'name', 'published', 'hinted', 'deficit'}}。読み取りのみ。"""
    rows = client.query(
        'SELECT c.id AS id, c.name_ja AS name, COUNT(sc.site_id) AS n FROM categories c '
        'LEFT JOIN site_categories sc ON sc.category_id=c.id '
        'WHERE NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id=c.id) GROUP BY c.id, c.name_ja')
    hinted = hinted_counts(client)
    rule = priority.rule_leaf_ids()   # 規則で埋める葉(国の機関・自治体など)は発見の対象にしない
    rows = [r for r in rows if r['id'] not in rule]
    return {r['id']: {'name': r.get('name'), 'published': int(r['n']), 'hinted': hinted.get(r['id'], 0),
                      'deficit': max(0, floor - int(r['n']))} for r in rows}


def hinted_counts(client):
    """hint_leaf つきで、まだ公開されていない取り込み中の URL 数(葉ごと)。url_candidates が無ければ空。"""
    marks = ','.join('?' * len(IN_PIPELINE))
    try:
        rows = client.query(
            'SELECT c.hint_leaf AS h, COUNT(*) AS n FROM url_candidates c JOIN url_progress p ON p.identity_key=c.identity_key '
            f'WHERE c.hint_leaf IS NOT NULL AND p.state IN ({marks}) GROUP BY c.hint_leaf', IN_PIPELINE)
    except RuntimeError as e:
        if 'no such table' in str(e):
            return {}
        raise
    return {r['h']: int(r['n']) for r in rows}


def plan(state):
    """不足のある葉を、不足の大きい順(同数は葉 id 順)に。人気・規模は見ない。"""
    return sorted((l for l, s in state.items() if s['deficit'] > 0), key=lambda l: (-state[l]['deficit'], l))


def covered(state, stats, leaf):
    """公開 + 取り込み中(hint つき。DB 上) + このランで新しく足した分。"""
    c = stats.leaves[leaf]
    s = state[leaf]
    return s['published'] + s['hinted'] + c['added'] + c['tagged']


def leaf_full(state, stats, leaf, floor=FLOOR):
    return covered(state, stats, leaf) >= COVER_FACTOR * floor


def init_leaves(state, stats, order):
    for leaf in order:
        stats.leaves[leaf]['deficit'] = state[leaf]['deficit']


# ------------------------------------------------------------------ Wikidata(葉の対応表)
def build_ids_query(row, limit, after):
    """対応表の 1 行 -> 公式サイト P856 を持つ項目 ID を、IRI の文字列順で after より後ろから limit 件(キーセット。知名度では絞らない)。"""
    cls = 'wdt:P31/wdt:P279*' if row['subclass'] == '1' and row['rel'] == 'P31' else f'wdt:{row["rel"]}'
    country = '|'.join(f'wdt:{p}' for p in row['country_prop'].split('|'))
    return (f'SELECT DISTINCT ?item WHERE {{ ?item {cls} wd:{row["qid"]} ; ({country}) wd:Q17 ; wdt:P856 [] . '
            f'FILTER(STR(?item) > "{ENTITY}{after}") }} ORDER BY STR(?item) LIMIT {int(limit)}')


def run_wikidata_leaves(sink, cursors, wdqs, rows, max_rows, client, urls_query, parse_ids, page_size=None, floor=FLOOR):
    """不足の大きい葉から順に、対応表の行ごとに続きから取って sink へ渡す(hint_leaf=その葉)。戻り値は中断理由(なければ None)。"""
    page_size = page_size or LEAF_PAGE
    source = 'wikidata-leaves'
    errs = dw.validate_map(rows)
    if errs:
        raise ValueError('対応表が不正: ' + '; '.join(errs))
    state = leaf_state(client, floor)
    order = [l for l in plan(state) if any(r['leaf_id'] == l for r in rows)]
    init_leaves(state, sink.stats, order)
    remaining, aborted = max_rows, None
    for leaf in order:
        for row in (r for r in rows if r['leaf_id'] == leaf):
            if remaining <= 0 or not sink.room(source) or leaf_full(state, sink.stats, leaf, floor):
                break
            cur = dw.cursor_name(row)
            cv = cursors.get(source, cur) or ''
            if cv == 'done':
                continue
            if cv and not _QID.match(cv):
                sink.stats.notes.append(f'{source} {cur}: カーソル値が不正({cv[:20]!r})。先頭から取り直す')
                cv = ''
            after, fetched, done_row, ok = cv, 0, False, True
            budget = min(int(row['max_rows']), remaining)
            while fetched < budget and sink.room(source) and not leaf_full(state, sink.stats, leaf, floor):
                room = COVER_FACTOR * floor - covered(state, sink.stats, leaf)
                limit = min(page_size, budget - fetched, max(room, MIN_PAGE))
                left = sink.room_left(source)
                if left is not None:
                    limit = min(limit, left)   # 上限で打ち切るページを作らない(カーソルより先の URL を捨てない)
                try:
                    ids = parse_ids(wdqs.query(build_ids_query(row, limit, after)))
                    got = dw.parse_results(wdqs.query(urls_query(ids))) if ids else []
                except dw.WdqsAbort as e:
                    aborted = str(e); ok = False; break
                except dw.WdqsSkip as e:
                    sink.stats.notes.append(f'{source} {cur}: skip {e}'); ok = False; break
                for _qid, url, _label in got:
                    sink.offer(source, leaf, url, hint_leaf=leaf)
                sink.flush()   # 入れて検証してから枠を数え直し、カーソルを進める
                if sink.stats.mismatch:
                    return aborted
                fetched += len(ids)
                if ids:
                    after = max(ids)
                    cursors.set(source, cur, after)
                if len(ids) < limit:
                    done_row = True; break
            remaining -= fetched
            if done_row and ok:
                cursors.set(source, cur, 'done')
            if aborted:
                return aborted
    return aborted


# ------------------------------------------------------------------ links(葉ごとのリンク元)
def run_links_leaf(sink, cursors, pf, client, max_sites, floor=FLOOR):
    """不足の大きい葉から順に、その葉の公開済みサイト(sites.id 昇順)のトップを取り、外部リンク先を hint_leaf=その葉で候補にする。"""
    st = sink.stats.rows['links']
    state = leaf_state(client, floor)
    order = plan(state)
    init_leaves(state, sink.stats, order)
    processed, fetched_ok, reasons = 0, 0, {}
    for leaf in order:
        if processed >= max_sites or not sink.room('links') or sink.stats.mismatch:
            break
        if leaf_full(state, sink.stats, leaf, floor):
            continue
        last = cursors.get('links-leaf', leaf) or ''
        stop = False
        while processed < max_sites and not stop and not leaf_full(state, sink.stats, leaf, floor):
            rows = client.query('SELECT s.id AS id, s.url AS url FROM sites s JOIN site_categories sc ON sc.site_id=s.id '
                                'WHERE sc.category_id=? AND s.id>? ORDER BY s.id LIMIT ?', (leaf, last, min(dlc.LINKS_PAGE, max_sites - processed)))
            if not rows:
                sink.stats.notes.append(f'links-leaf {leaf}: 公開済みのリンク元を最後まで読んだ(続き id>{last!r})')
                break
            for row in rows:
                if not sink.room('links') or sink.stats.mismatch or leaf_full(state, sink.stats, leaf, floor):
                    stop = True
                    break
                before = st['over_limit']
                html, reason = pf.get(row['url'] or '')
                if html is None:
                    reason = reason or 'unknown'
                    reasons[reason] = reasons.get(reason, 0) + 1
                else:
                    fetched_ok += 1
                    for u in dlc.extract_outbound(html, row['url']):
                        sink.offer('links', 'outbound-link', u, keep_path=urllib.parse.urlsplit(u).path != '/', hint_leaf=leaf)
                sink.flush()   # 入れて検証してからカーソルを進める
                if st['over_limit'] > before or sink.stats.mismatch:
                    stop = True   # 上限で捨てた候補がある。このサイトは次回やり直す
                    break
                last = str(row['id'])
                cursors.set('links-leaf', leaf, last)
                processed += 1
    sink.stats.notes.append(f'links-leaf: 処理 {processed} サイト(取得できた {fetched_ok} / 取得できず {sum(reasons.values())} {dict(sorted(reasons.items()))})')   # 「処理 0 サイト」は終端判定に使われる。書式を変えない


# ------------------------------------------------------------------ レポート
def report_leaves(stats, apply, out=print):
    """葉ごとの表: 不足 / 取得した URL 数 / 既知 / 追加 / 既存行へ付けた hint。"""
    if not stats.leaves:
        return
    out(f'== leaf (mode: {"APPLY" if apply else "DRY-RUN: added は追加予定"}) 不足の大きい順')
    out(f'{"leaf":28} {"deficit":>8} {"fetched":>8} {"known":>6} {"added":>6} {"tagged":>7}')
    for leaf, c in sorted(stats.leaves.items(), key=lambda kv: (-kv[1]['deficit'], kv[0])):
        out(f'{leaf:28} {c["deficit"]:>8} {c["fetched"]:>8} {c["known"]:>6} {c["added"]:>6} {c["tagged"]:>7}')


def report_unmapped(client, rows, out=print, floor=FLOOR):
    """Wikidata の対応が無く、不足がある葉の一覧(個人のテキスト系ジャンルなど)。URL は推測しない。人が見るための報告。"""
    state = leaf_state(client, floor)
    mapped = {r['leaf_id'] for r in rows}
    todo = [l for l in plan(state) if l not in mapped]
    out(f'== Wikidata の対応が無く、公開が {floor} 件未満の葉: {len(todo)} 葉(不足の大きい順)')
    out(f'{"leaf":28} {"published":>9} {"deficit":>8}  name')
    for l in todo:
        s = state[l]
        out(f'{l:28} {s["published"]:>9} {s["deficit"]:>8}  {s["name"] or ""}')
    if todo:
        out(f'suggest: {SUGGESTION}')
    return todo
