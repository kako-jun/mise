#!/usr/bin/env python3
"""オンライン補強ランナー: 取得 -> 判定 -> 掲載を本番 D1 だけを正本にして回す(scripts/enrich/ の 3 段を束ねる)。

ローカルに状態を持たない(ファイル・チェックポイント・ログを作らない)。出力は標準出力だけ。
続きの位置は url_progress の状態そのもの、排他は D1 discovery_cursors のリース(source='lease', cursor_key='enrich')。

  uv run python scripts/enrich_online.py [--stages fetch,judge,publish] [--limit N | --fetch-limit/--judge-limit/--publish-limit N]
                                         [--rounds N] [--max-seconds S] [--budget-month USD] [--judge-concurrency 1..4] [--dry-run | --apply]

既定は --dry-run(読むだけ。1 回だけ回し、各段の対象件数を出す)。--apply で書く。--rounds 0 は「残りが無くなるか停止条件まで」。
終了コード: 0 正常(残り無し・予算切れ・--max-seconds・--rounds 到達・1 回の上限) / 1 検証不一致・整合チェック違反・異常ガード・
            前提チェック不合格・判定段が止まった / 2 リースが取れない・引数不正 / 3 レート制限(429) / 4 実行中にリースを失った。
"""
import argparse
import collections
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import discover_online as do
from enrich import fetch_stage, judge_stage, publish_stage

STAGES = ('fetch', 'judge', 'publish')
DEFAULT_LIMITS = {'fetch': 60, 'judge': 30, 'publish': 60}
STATES = ('discovered', 'queued', 'fetched', 'reviewing', 'candidate', 'approved', 'rejected', 'failed', 'blocked', 'published')
LEASE_KEY = 'enrich'
LEASE_TTL = 1800
REQUEST_INTERVAL = 0.3

# --- 異常ガード(旧 nightly.py の「前日比の異常で本番反映を止める」の代わり。単純な定数) ---
PUBLISH_MAX_ERRORS = 5          # 1 回の掲載段のバッチ失敗数がこれ以上なら止める
PUBLISH_MAX_ERROR_RATIO = 0.5   # 選んだ行のうちバッチ失敗がこの割合を超えたら止める(選択 4 件以上)
PUBLISH_MAX_HELD_RATIO = 0.5    # 選んだ行のうちゲート保留がこの割合を超えたら止める(選択 10 件以上)
JUDGE_MIN_SAMPLE = 10           # 判定結果の割合を見る最小件数
JUDGE_MAX_NONAPPROVE_RATIO = 0.95   # 判定した行のうち approved 以外(rejected+candidate)がこの割合を超えたら、その回の掲載を行わず止める
MAX_PUBLISH_PER_RUN = 1000      # 1 回のランナー実行で掲載する上限(超えたら正常終了。残りは次回)
STALE_REVIEWING_MINUTES = 30    # docs/operations/enrich.md の前提チェックと同じ


class Stop(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def is_rate_limit(e):
    return any(t in str(e) for t in ('429', 'rate limit', 'Too Many Requests'))


def progress_counts(client):
    rows = client.query('SELECT state, COUNT(*) AS n FROM url_progress GROUP BY state')
    out = {s: 0 for s in STATES}
    for r in rows:
        out[r['state']] = out.get(r['state'], 0) + int(r['n'])
    return out


def month_spent(client):
    rows = client.query(f'SELECT COALESCE(SUM(cost_usd),0) AS usd FROM llm_ledger WHERE {judge_stage.MONTH_WHERE}')
    return float(rows[0]['usd']) if rows else 0.0


def stale_reviewing(client):
    """docs/operations/enrich.md の前提: 判定を持たない古い reviewing(旧 export の写像が作った行)。
    判定段自身の中断した claim(取得段が 'fetched' にした行 = url_candidates.state='fetched')は判定段が 30 分後に取り直すので数えない。"""
    return [r['identity_key'] for r in client.query(
        "SELECT p.identity_key FROM url_progress p JOIN url_candidates c USING(identity_key) WHERE p.state='reviewing' "
        f"AND p.updated_at < datetime('now','-{STALE_REVIEWING_MINUTES} minutes') AND (c.review_state IS NULL OR c.review_state='') "
        "AND COALESCE(c.state,'')<>'fetched' ORDER BY p.identity_key LIMIT 50")]


def time_travel_bookmark(client):
    """Cloudflare REST: GET .../d1/database/{db}/time_travel/bookmark。-> bookmark 文字列。失敗は例外。"""
    url = f'https://api.cloudflare.com/client/v4/accounts/{client.account}/d1/database/{client.database}/time_travel/bookmark'
    res = client._request(url)
    bm = (res.get('result') or {}).get('bookmark')
    if not bm:
        raise RuntimeError('response has no bookmark')
    return bm


def table(title, stats, out):
    out(f'--- {title} ---')
    for k, v in stats.items():
        if isinstance(v, (dict, list)) and not v:
            continue
        out(f'  {k:<22} {v}')


def fmt_summary(start, end, out):
    out('--- url_progress ---')
    out(f'  {"state":<12}{"start":>9}{"end":>9}{"delta":>9}')
    for s in sorted(set(start) | set(end), key=lambda x: STATES.index(x) if x in STATES else 99):
        a, b = start.get(s, 0), end.get(s, 0)
        out(f'  {s:<12}{a:>9}{b:>9}{b - a:>+9}')


def preflight(client, apply, bookmark_fn, out):
    counts = progress_counts(client)
    sites = client.query('SELECT COUNT(*) AS n FROM sites')[0]['n']
    out('pre-flight: ' + ', '.join(f'{k}={v}' for k, v in counts.items() if v) + f', sites={sites}')
    out(f'pre-flight: LLM spend this month (UTC) = ${month_spent(client):.4f} (cap ${judge_stage.MONTHLY_CAP_USD:.2f})')
    bad = stale_reviewing(client)
    if bad:
        msg = (f'pre-flight: 判定を持たない古い reviewing が {len(bad)}+ 件ある(旧データを LLM に送ってしまうため開始しない)。'
               f'docs/operations/enrich.md「実行と排他」の前提を確認: {bad[:10]}')
        if apply:
            raise Stop(1, msg)
        out('WARNING ' + msg)
    if apply:
        try:
            out(f'pre-flight: Time Travel bookmark = {bookmark_fn(client)}')
        except Exception as e:  # 取れなくても続ける(判断は実行する人)
            out(f'WARNING pre-flight: Time Travel bookmark を取得できなかった({type(e).__name__}: {str(e)[:120]})。復元点なしで続行')
    return counts


def run_round(client, n, active, limits, apply, out, *, budget_month, concurrency, fetch, llm, lease, totals):
    """1 回分。-> (その回の各段の stats, 何か選ばれたか)。止める時は Stop。"""
    res, selected, hold_publish = {}, 0, None

    def guard_lease():
        if apply:
            lease.renew()   # LeaseLost は呼び出し側で exit 4

    if 'fetch' in active:
        guard_lease()
        res['fetch'] = fetch_stage.run(client, limit=limits['fetch'], apply=apply, log=out, fetch=fetch, workers=fetch_stage.WORKERS)
        selected += res['fetch'].get('selected', 0)
        if res['fetch'].get('verify_failed'):
            raise Stop(1, f'fetch: 読み戻し検証が {res["fetch"]["verify_failed"]} 件不一致(ログの verify failed の行を確認)')
    if 'judge' in active:
        guard_lease()
        s = judge_stage.run(client, limit=limits['judge'], apply=apply, log=out, llm=llm, budget_month=budget_month, concurrency=concurrency)
        res['judge'] = s
        selected += s.get('selected', 0)
        if s.get('budget_stopped'):
            active.discard('judge')
            out(f'judge: 予算上限に達したため判定段を止める({s.get("stopped")})。他の段は続ける')
        elif s.get('stopped'):
            raise Stop(1, f'judge が止まった: {s["stopped"]}')
        judged = s.get('judged', 0)
        nonappr = s.get('rejected', 0) + s.get('candidate', 0)
        if apply and judged >= JUDGE_MIN_SAMPLE and nonappr / judged > JUDGE_MAX_NONAPPROVE_RATIO:
            hold_publish = f'judge の結果が異常: {judged} 件中 approved 以外が {nonappr} 件(> {JUDGE_MAX_NONAPPROVE_RATIO:.0%})'
    if 'publish' in active:
        guard_lease()
        if hold_publish:
            table('publish', {'skipped': hold_publish}, out)
            raise Stop(1, hold_publish + '。この回の掲載を行わず停止')
        limit = limits['publish']
        if apply:
            limit = min(limit, max(0, MAX_PUBLISH_PER_RUN - totals['published']))
        p = publish_stage.run(client, limit=limit, apply=apply, log=out) if limit > 0 else {'selected': 0, 'published': 0, 'ids': [], 'integrity': {}, 'verify_problems': {}}
        res['publish'] = p
        selected += p.get('selected', 0)
        totals['published'] += p.get('published', 0)
        if p.get('verify_problems') or p.get('stopped'):
            raise Stop(1, f'publish: 読み戻し検証の不一致 {p.get("verify_problems")}')
        if p.get('integrity'):
            ids = sorted({str(t[0]) for rows in p['integrity'].values() for t in rows if t})
            raise Stop(1, f'publish: 整合チェック違反 {list(p["integrity"])} 対象: {ids[:20]}(Time Travel の bookmark で復元を検討)')
        sel, err, held = p.get('selected', 0), p.get('errors', 0), p.get('held', 0)
        if apply and (err >= PUBLISH_MAX_ERRORS or (sel >= 4 and err / sel > PUBLISH_MAX_ERROR_RATIO)):
            raise Stop(1, f'publish: バッチ失敗が異常({err}/{sel})')
        if apply and sel >= 10 and held / sel > PUBLISH_MAX_HELD_RATIO:
            raise Stop(1, f'publish: ゲート保留が異常({held}/{sel} > {PUBLISH_MAX_HELD_RATIO:.0%})。保留理由を確認')
    for k, v in res.items():
        table(f'round {n} {k}', {a: b for a, b in v.items() if a != 'ids'}, out)
    return res, selected


def run_pipeline(client, *, stages=STAGES, limits=None, rounds=0, max_seconds=None, apply=False, budget_month=judge_stage.MONTHLY_CAP_USD,
                 concurrency=1, out=print, fetch=None, llm=None, lease=None, clock=time.monotonic, bookmark_fn=time_travel_bookmark):
    """-> (終了コード, 最初と最後の状態別件数)。テストは fetch / llm / lease / clock / bookmark_fn を差し込む。"""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    active = {s for s in STAGES if s in stages}
    lease = lease or do.Lease(client, ttl=LEASE_TTL, key=LEASE_KEY)
    if not apply:
        rounds = 1   # dry-run は状態が変わらないので繰り返さない
    if apply and not lease.acquire():
        out('lease: 別の実行が動いている。何もせず終了')
        return 2, None, None
    started, start, end, code = clock(), None, None, 0
    totals = collections.Counter()
    try:
        start = preflight(client, apply, bookmark_fn, out)
        n, prev = 0, start
        while True:
            n += 1
            res, selected = run_round(client, n, active, limits, apply, out, budget_month=budget_month, concurrency=concurrency,
                                      fetch=fetch, llm=llm, lease=lease, totals=totals)
            cur = progress_counts(client)
            if apply and cur == prev and selected:
                out('停止: この回で url_progress が 1 件も動かなかった(対象はあるが進まない。ログを確認)')
                break
            prev = cur
            if not apply:
                break
            if not selected:
                out('残りなし: 全段で対象が 0 件')
                break
            if apply and totals['published'] >= MAX_PUBLISH_PER_RUN:
                out(f'停止: 1 回の実行の掲載上限 {MAX_PUBLISH_PER_RUN} 件に達した(残りは次回)')
                break
            if rounds and n >= rounds:
                out(f'停止: --rounds {rounds} に到達')
                break
            if max_seconds is not None and clock() - started >= max_seconds:
                out(f'停止: --max-seconds {max_seconds} に到達')
                break
            if not active:
                out('残りなし: 動かせる段が無い')
                break
    except do.LeaseLost as e:
        out(f'lease lost: {e}')
        code = do.LEASE_LOST_EXIT
    except judge_stage.LedgerSettleError as e:
        out(f'停止: 費用台帳を確定できなかった。予約(reserve)はそのまま残っている: {e}。docs/operations/enrich.md「残った reserve 行の解消」を確認')
    except Stop as e:
        out(f'停止(exit {e.code}): {e.message}')
        code = e.code
    except Exception as e:
        if is_rate_limit(e):
            out(f'停止: レート制限({type(e).__name__}: {str(e)[:200]})。間隔を空けて再実行')
            code = 3
        else:
            raise
    finally:
        if apply:
            try:
                lease.release()
            except Exception as e:  # 解放の失敗で元のエラーを隠さない
                out(f'lease release failed: {e}')
        try:
            if start is not None:
                end = progress_counts(client)
                fmt_summary(start, end, out)
        except Exception as e:
            out(f'summary failed: {e}')
    return code, start, end


def main(argv=None, *, client=None, **inject):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stages', default=','.join(STAGES), help='fetch,judge,publish のうち回す段(カンマ区切り)')
    ap.add_argument('--limit', type=int, default=None, help='全段共通の 1 回あたりの件数(段別指定が優先)')
    for s in STAGES:
        ap.add_argument(f'--{s}-limit', type=int, default=None, help=f'{s} 段の 1 回あたりの件数(既定 {DEFAULT_LIMITS[s]})')
    ap.add_argument('--rounds', type=int, default=0, help='回数。0 = 残りが無くなるか停止条件まで')
    ap.add_argument('--max-seconds', type=float, default=None, help='この秒数を超えたら次の回に入らず終了')
    ap.add_argument('--budget-month', type=float, default=judge_stage.MONTHLY_CAP_USD, help='月の LLM 費用の上限(USD)')
    ap.add_argument('--judge-concurrency', type=int, default=1, help='判定段の並列数(1〜4)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='既定。何も書かない')
    mode.add_argument('--apply', action='store_true', help='本番 D1 へ書く')
    a = ap.parse_args(argv)
    stages = [s.strip() for s in a.stages.split(',') if s.strip()]
    if not stages or any(s not in STAGES for s in stages):
        ap.error(f'--stages は {",".join(STAGES)} のカンマ区切り')
    limits = {s: getattr(a, f'{s}_limit') or a.limit or DEFAULT_LIMITS[s] for s in STAGES}
    if min(limits.values()) < 1 or a.rounds < 0 or (a.max_seconds is not None and a.max_seconds <= 0) or not 1 <= a.judge_concurrency <= 4 or a.budget_month <= 0:
        ap.error('--limit は 1 以上、--rounds は 0 以上、--max-seconds は正、--judge-concurrency は 1〜4、--budget-month は正')
    if client is None:
        import media_online
        client = media_online.Cloudflare(min_request_interval=REQUEST_INTERVAL)
    code, _, _ = run_pipeline(client, stages=stages, limits=limits, rounds=a.rounds, max_seconds=a.max_seconds, apply=a.apply,
                              budget_month=a.budget_month, concurrency=a.judge_concurrency, **inject)
    return code


if __name__ == '__main__':
    sys.exit(main())
