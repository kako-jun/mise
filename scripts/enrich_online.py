#!/usr/bin/env python3
"""オンライン補強ランナー: 取得 -> 判定 -> 合議 -> 掲載を本番 D1 だけを正本にして回す(scripts/enrich/ の 4 段を束ねる)。

ローカルに状態を持たない(ファイル・チェックポイント・ログを作らない)。出力は標準出力だけ。
続きの位置は url_progress の状態そのもの、排他は D1 discovery_cursors のリース(source='lease', cursor_key='enrich')。

  uv run python scripts/enrich_online.py [--stages fetch,judge,deliberate,requalify,publish] [--limit N | --fetch-limit/--judge-limit/--deliberate-limit/--publish-limit N]
                                         [--rounds N] [--max-seconds S] [--budget-month USD] [--judge-concurrency 1..4] [--priority leaf|none] [--dry-run | --apply]

既定は --dry-run(読むだけ。1 回だけ回し、各段の対象件数を出す)。--apply で書く。--rounds 0 は「残りが無くなるか停止条件まで」。
終了コード: 0 正常(残り無し・予算切れ・--max-seconds・--rounds 到達・日次の掲載上限) / 1 検証不一致・整合チェック違反・異常ガード・
            前提チェック不合格・判定段が止まった・判定の費用ガード / 2 リースが取れない・引数不正 / 3 レート制限(429) /
            4 実行中にリースを失った / 5 費用台帳を確定できない(人の対応が必要。スケジューラは再試行しない)。
"""
import argparse
import re
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import discover_online as do
from enrich import deliberate_stage, fetch_stage, judge_stage, priority as prio, publish_stage

STAGES = ('fetch', 'judge', 'deliberate', 'requalify', 'publish')
DEFAULT_LIMITS = {'fetch': 60, 'judge': 30, 'deliberate': 20, 'requalify': 20, 'publish': 60}
STATES = ('discovered', 'queued', 'fetched', 'reviewing', 'candidate', 'approved', 'rejected', 'failed', 'blocked', 'published')
LEASE_KEY = 'enrich'
LEASE_TTL = 1800
HEARTBEAT_SECONDS = 120         # 段の内部(判定・取得・掲載)でリースを更新する間隔
MAX_LIMIT = 200                 # 1 段・1 回あたりの件数の上限(これを超える指定は引数エラー。リース TTL に収める)
LEDGER_EXIT = 5
REQUEST_INTERVAL = 0.3

# --- 異常ガード(旧 nightly.py の「前日比の異常で本番反映を止める」の代わり。単純な定数) ---
PUBLISH_MAX_ERRORS = 5          # 1 回の掲載段のバッチ失敗数がこれ以上なら止める
PUBLISH_MAX_ERROR_RATIO = 0.5   # 選んだ行のうちバッチ失敗がこの割合を超えたら止める(選択 4 件以上)
PUBLISH_MAX_HELD_RATIO = 0.5    # 選んだ行のうちゲート保留がこの割合を超えたら止める(選択 10 件以上)
JUDGE_GUARD_WINDOW = 100        # 判定の費用ガード: 直近 JUDGE_GUARD_HOURS 時間の判定がこの件数以上あり approved が 0 件なら、LLM を呼ばない(掲載・取得・LLM 無しの移動は続ける)
JUDGE_GUARD_HOURS = 6           # 窓の長さ。この時間たつと窓が空になり自然に再開する
DELIB_GUARD_MIN_ROWS = 10      # 合議段の異常ガード: 選んだ行がこれ以上で、猫の失敗(会議中に落ちた行のうち失敗が原因のもの)が半分を超えたら止める
DELIB_GUARD_FAIL_RATIO = 0.5
STALE_RESERVE_HOURS = 6         # これより古い llm_ledger の reserve 行が残っていれば開始しない(exit 5)
HEARTBEAT_MAX_FAILS = 3         # heartbeat の更新が(リース喪失ではなく)D1 の一時エラーで連続この回数失敗したらリース喪失扱い
MAX_PUBLISH_PER_DAY = 5000      # 既定。直近 24 時間に作られた sites(D1 から数える)がこの件数に達したら掲載しない(正常終了。残りは翌日以降)。--max-publish-per-day で変更
STALE_REVIEWING_MINUTES = 30    # docs/operations/enrich.md の前提チェックと同じ


class Stop(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def is_rate_limit(e):
    """例外(と原因の連鎖)が HTTP ステータス 429 を持てばそれ。無ければ文言 'HTTP 429' / 'API 429' / 'rate limit' だけ。"""
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        for attr in ('code', 'status', 'status_code'):
            if getattr(e, attr, None) == 429:
                return True
        if re.search(r'HTTP 429|API 429|rate.?limit', str(e), re.I):
            return True
        e = e.__cause__ or e.__context__
    return False


class Heartbeat:
    """段の内部から呼ばれる。HEARTBEAT_SECONDS ごとにリースを更新し、失っていれば LeaseLost を上げる(スレッドから呼ばれうる)。"""
    def __init__(self, lease, clock, interval=HEARTBEAT_SECONDS):
        self.lease, self.clock, self.interval = lease, clock, interval
        self.last = clock()
        self.fails = 0
        self.lock = threading.Lock()

    def _renew(self):
        try:
            self.lease.renew()
        except do.LeaseLost:
            raise
        except Exception:   # D1 の一時エラー: 次の拍で再試行し、連続で失敗したときだけリース喪失扱い
            self.fails += 1
            if self.fails >= HEARTBEAT_MAX_FAILS:
                raise do.LeaseLost(f'リースを {self.fails} 回連続で更新できなかった')
            return
        self.fails = 0
        self.last = self.clock()

    def __call__(self):
        with self.lock:
            if self.clock() - self.last >= self.interval:
                self._renew()

    def now(self):
        with self.lock:
            self._renew()


def published_last_day(client):
    return int(client.query("SELECT COUNT(*) AS n FROM sites WHERE registered_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')")[0]['n'])


def judge_unapproved_window(client):
    """直近 JUDGE_GUARD_HOURS 時間(D1 時刻)の判定が JUDGE_GUARD_WINDOW 件以上あり、approved が 0 件か。"""
    r = client.query("SELECT COUNT(*) AS n, COALESCE(SUM(review_state='approved'),0) AS ok FROM url_candidates "
                     f"WHERE judged_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-{int(JUDGE_GUARD_HOURS)} hours')")[0]
    return int(r['n']) >= JUDGE_GUARD_WINDOW and int(r['ok']) == 0


def stale_reserves(client):
    return int(client.query("SELECT COUNT(*) AS n FROM llm_ledger WHERE kind='reserve' "
                            f"AND ts < strftime('%Y-%m-%dT%H:%M:%SZ','now','-{int(STALE_RESERVE_HOURS)} hours')")[0]['n'])


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
    """docs/operations/enrich.md の前提: reviewing かつ review_state が空(旧 export の写像が作った行)。判定段自身が作る reviewing も
    review_state は空だが updated_at が新しいので、30 分以内に更新された行は数えない(それより古いものが残っていれば止まる)。"""
    return [r['identity_key'] for r in client.query(
        "SELECT p.identity_key FROM url_progress p JOIN url_candidates c USING(identity_key) WHERE p.state='reviewing' "
        f"AND p.updated_at < datetime('now','-{STALE_REVIEWING_MINUTES} minutes') AND (c.review_state IS NULL OR c.review_state='') "
        "ORDER BY p.identity_key LIMIT 50")]


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


def preflight(client, apply, bookmark_fn, out, judge_active=True, llm_active=None):
    llm_active = judge_active if llm_active is None else llm_active   # reserve 行の検査は LLM を使う段(判定・合議)があるとき
    counts = progress_counts(client)
    sites = client.query('SELECT COUNT(*) AS n FROM sites')[0]['n']
    out('pre-flight: ' + ', '.join(f'{k}={v}' for k, v in counts.items() if v) + f', sites={sites}')
    out(f'pre-flight: LLM spend this month (UTC) = ${month_spent(client):.4f} (cap ${judge_stage.MONTHLY_CAP_USD:.2f})')
    odd = client.query("SELECT COUNT(*) AS n FROM url_progress p JOIN url_candidates c USING(identity_key) WHERE p.state='approved' "
                       "AND COALESCE(c.review_state,'')<>'approved'")[0]['n']
    out(f'pre-flight: url_progress.state=approved なのに review_state が approved でない行 = {odd}(掲載段は選ばない)')
    if llm_active:
        n_res = stale_reserves(client)
        if n_res:
            msg = (f'pre-flight: {STALE_RESERVE_HOURS} 時間より古い reserve 行が {n_res} 件ある(費用台帳が未解消)。'
                   'docs/operations/enrich.md「残った reserve 行の解消」を人が済ませるまで開始しない')
            if apply:
                raise Stop(LEDGER_EXIT, msg)
            out('WARNING ' + msg)
    bad = stale_reviewing(client) if judge_active else []   # 判定段を含む実行のときだけ
    if bad:
        msg = (f'pre-flight: 判定を持たない古い reviewing が {len(bad)}+ 件ある(旧データを LLM に送ってしまうため開始しない)。'
               f'docs/operations/enrich.md「実行と排他」の前提を確認: {bad[:10]}')
        if apply:
            raise Stop(1, msg)
        out('WARNING ' + msg)
    cols = {r['name'] for r in client.query('PRAGMA table_info(url_candidates)')}
    if 'site_name' not in cols:
        msg = 'pre-flight: url_candidates.site_name が無い。migration 0040 を先に本番へ適用してから実行する'
        if apply:
            raise Stop(1, msg)
        out('WARNING ' + msg)
    if apply:
        try:
            out(f'pre-flight: Time Travel bookmark = {bookmark_fn(client)}')
        except Exception as e:  # 取れなくても続ける(判断は実行する人)
            out(f'WARNING pre-flight: Time Travel bookmark を取得できなかった({type(e).__name__}: {str(e)[:120]})。復元点なしで続行')
    return counts


def integrity_stop(p):
    """整合チェック違反の Stop(違反の種類と行、この回に掲載した id を出す。例外型名ではなく id)。"""
    integ = dict(p.get('integrity') or {})
    failed = integ.pop('post_check_failed', None)
    ids = list(p.get('ids') or [])
    parts = []
    if failed:
        parts.append(f'整合チェック自体が失敗({failed[0][1] if failed and len(failed[0]) > 1 else failed})。この回に掲載した id を確認')
    for name, rows in integ.items():
        parts.append(f'{name}: {[list(map(str, r)) for r in rows[:3]]}')
    return Stop(1, f'publish: 整合チェック違反 {"; ".join(parts)} / この回に掲載した id: {ids[:20]}(Time Travel の bookmark で復元を検討)')


def run_round(client, n, active, limits, apply, out, *, budget_month, concurrency, max_publish_per_day, fetch, llm, lease, hb, state):
    """1 回分。-> (その回の各段の stats, 何か選ばれたか)。止める時は Stop。state['final'] は最後に返す終了コード(判定の費用ガード)。"""
    res, selected = {}, 0

    def guard_lease():
        if apply:
            hb.now()   # LeaseLost は呼び出し側で exit 4

    beat = hb if apply else None
    if 'fetch' in active:
        guard_lease()
        res['fetch'] = fetch_stage.run(client, limit=limits['fetch'], apply=apply, log=out, fetch=fetch, workers=fetch_stage.WORKERS, heartbeat=beat,
                                    priority=state['sel']['fetch'])
        selected += res['fetch'].get('selected', 0)
        if res['fetch'].get('verify_failed'):
            raise Stop(1, f'fetch: 読み戻し検証が {res["fetch"]["verify_failed"]} 件不一致(ログの verify failed の行を確認)')
    llm_on = True
    if ({'judge', 'deliberate'} & active) and apply and judge_unapproved_window(client):
        llm_on = False
        if not state.get('guard_said'):
            state['guard_said'] = True
            out(f'停止(最後に exit 1): judge: 直近 {JUDGE_GUARD_HOURS} 時間の判定が {JUDGE_GUARD_WINDOW} 件以上あり approved が 0 件。'
                'LLM を呼ばない(費用を使い続けないため。LLM を使わない移動・取得・掲載は続ける)。判定の内容を確認')
        state['final'] = 1
    if 'judge' in active:
        guard_lease()
        s = judge_stage.run(client, limit=limits['judge'], apply=apply, log=out, llm=llm, budget_month=budget_month, concurrency=concurrency,
                            heartbeat=beat, llm_enabled=llm_on, priority=state['sel']['judge'])
        res['judge'] = s
        selected += s.get('selected', 0) if llm_on else s.get('moved_without_llm', 0)
        if s.get('budget_stopped'):
            active.discard('judge')
            out(f'judge: 予算上限に達したため判定段を止める({s.get("stopped")})。他の段は続ける')
        elif s.get('stopped'):
            raise Stop(3 if is_rate_limit(RuntimeError(str(s['stopped']))) else 1, f'judge が止まった: {s["stopped"]}')
    if 'deliberate' in active:
        guard_lease()
        d = deliberate_stage.run(client, limit=limits['deliberate'], apply=apply, log=out, llm=llm, budget_month=budget_month, concurrency=concurrency,
                                 heartbeat=beat, llm_enabled=llm_on)
        res['deliberate'] = d
        selected += d.get('selected', 0) if llm_on else 0
        state['delib_progress'] = bool(d.get('deliberated'))   # 会議中だけの回は url_progress が動かない。それは進捗として数える
        if d.get('budget_stopped'):
            active.discard('deliberate')
            out(f'deliberate: 予算上限に達したため合議段を止める({d.get("stopped")})。他の段は続ける')
        elif d.get('stopped'):
            raise Stop(3 if is_rate_limit(RuntimeError(str(d['stopped']))) else 1, f'deliberate が止まった: {d["stopped"]}')
        if apply and d.get('selected', 0) >= DELIB_GUARD_MIN_ROWS and d.get('cat_failed', 0) / d['selected'] > DELIB_GUARD_FAIL_RATIO:
            raise Stop(1, f'deliberate: 猫の失敗が異常({d.get("cat_failed")}/{d["selected"]})。ログの理由を確認')
    if 'requalify' in active:
        guard_lease()
        rq = judge_stage.requalify(client, limit=limits['requalify'], apply=apply, log=out, llm=llm, budget_month=budget_month, heartbeat=beat, llm_enabled=llm_on)
        res['requalify'] = rq
        selected += rq.get('selected', 0) if llm_on else 0
        if rq.get('budget_stopped'):
            active.discard('requalify')
            out(f'requalify: 予算上限に達したため再資格化を止める({rq.get("stopped")})。他の段は続ける')
        elif rq.get('stopped'):
            raise Stop(3 if is_rate_limit(RuntimeError(str(rq['stopped']))) else 1, f'requalify が止まった: {rq["stopped"]}')
    if 'publish' in active:
        guard_lease()
        limit = limits['publish']
        if apply:
            room = max_publish_per_day - published_last_day(client)
            limit = min(limit, max(0, room))
            if limit <= 0:
                out(f'掲載しない: 直近 24 時間に作られた sites が日次上限 {max_publish_per_day} 件に達した')
                state['daily_cap'] = True
        if limit > 0:
            try:
                p = publish_stage.run(client, limit=limit, apply=apply, log=out, heartbeat=beat)
            except do.LeaseLost:
                raise
            except Exception as e:
                part = getattr(e, 'result', None)
                if part and part.get('integrity'):
                    out(f'publish が例外で中断: {type(e).__name__}')
                    raise integrity_stop(part) from e
                raise
        else:
            p = {'selected': 0, 'published': 0, 'ids': [], 'integrity': {}, 'verify_problems': {}}
        res['publish'] = p
        if limit > 0:
            selected += p.get('selected', 0)
        if p.get('verify_problems') or p.get('stopped'):
            raise Stop(1, f'publish: 読み戻し検証の不一致 {p.get("verify_problems")}')
        if p.get('integrity'):
            raise integrity_stop(p)
        sel, err, held = p.get('selected', 0), p.get('errors', 0), p.get('held', 0)
        held -= sum(v for k, v in (p.get('held_reasons') or {}).items() if k.startswith('gate:quality_'))   # 品質の保留は requalify 段が直す。異常の比率に数えない
        if apply and (err >= PUBLISH_MAX_ERRORS or (sel >= 4 and err / sel > PUBLISH_MAX_ERROR_RATIO)):
            raise Stop(1, f'publish: バッチ失敗が異常({err}/{sel})')
        if apply and sel >= 10 and held / sel > PUBLISH_MAX_HELD_RATIO:
            raise Stop(1, f'publish: ゲート保留が異常({held}/{sel} > {PUBLISH_MAX_HELD_RATIO:.0%})。保留理由を確認')
    for k, v in res.items():
        table(f'round {n} {k}', {a: b for a, b in v.items() if a != 'ids'}, out)
    return res, selected


def run_pipeline(client, *, stages=STAGES, limits=None, rounds=0, max_seconds=None, apply=False, budget_month=judge_stage.MONTHLY_CAP_USD,
                 concurrency=1, max_publish_per_day=None, out=print, fetch=None, llm=None, lease=None, clock=time.monotonic, bookmark_fn=time_travel_bookmark, priority='none'):   # 既定 none = 関数を直接呼ぶ側(テスト等)の挙動を変えない。CLI の既定は leaf
    """-> (終了コード, 最初と最後の状態別件数)。テストは fetch / llm / lease / clock / bookmark_fn を差し込む。"""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    if any(not 1 <= v <= MAX_LIMIT for v in limits.values()):
        raise ValueError(f'各段の limit は 1〜{MAX_LIMIT}')
    if max_publish_per_day is None:
        max_publish_per_day = MAX_PUBLISH_PER_DAY
    if max_publish_per_day < 1:
        raise ValueError('max_publish_per_day は 1 以上')
    active = {s for s in STAGES if s in stages}
    lease = lease or do.Lease(client, ttl=LEASE_TTL, key=LEASE_KEY)
    if not apply:
        rounds = 1   # dry-run は状態が変わらないので繰り返さない
    if apply and not lease.acquire():
        out('lease: 別の実行が動いている。何もせず終了')
        return 2, None, None
    started, start, end, code = clock(), None, None, 0
    state = {'final': 0, 'daily_cap': False, 'delib_progress': False, 'sel': {s: prio.Selector(priority) for s in ('fetch', 'judge')}}
    hb = Heartbeat(lease, clock)
    try:
        start = preflight(client, apply, bookmark_fn, out, judge_active='judge' in active, llm_active=bool({'judge', 'deliberate', 'requalify'} & active))
        n, prev = 0, start
        while True:
            n += 1
            res, selected = run_round(client, n, active, limits, apply, out, budget_month=budget_month, concurrency=concurrency,
                                      max_publish_per_day=max_publish_per_day, fetch=fetch, llm=llm, lease=lease, hb=hb, state=state)
            cur = progress_counts(client)
            if apply and cur == prev and selected and not state.get('delib_progress'):
                out('WARNING 停止: この回で url_progress が 1 件も動かなかった(対象はあるが進まない。ログを確認)。終了コードは 0')
                break
            prev = cur
            if not apply:
                break
            if state['daily_cap']:
                out(f'停止: 日次の掲載上限 {max_publish_per_day} 件(残りは翌日以降)')
                break
            if not selected:
                out('残りなし: 全段で対象が 0 件')
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
        code = state['final']
    except do.LeaseLost as e:
        out(f'lease lost: {e}')
        code = do.LEASE_LOST_EXIT
    except judge_stage.LedgerSettleError as e:
        out(f'停止(exit {LEDGER_EXIT}・再試行しない): 費用台帳を確定できなかった。予約(reserve)はそのまま残っている: {e}。'
            'docs/operations/enrich.md「残った reserve 行の解消」を人が確認するまで再実行しない')
        code = LEDGER_EXIT
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


def one_shot(fn, client, apply, lease=None, out=print):
    """--backfill-empty / --reopen-empty: 1 回だけ実行して終了する(--apply のときだけ書き、リースを取る。途中のハートビートは無く、リース TTL に収まる件数の前提)。"""
    lease = lease or do.Lease(client, ttl=LEASE_TTL, key=LEASE_KEY)
    if apply and not lease.acquire():
        out('lease: 別の実行が動いている。何もせず終了')
        return 2
    try:
        fn(client, apply, log=out)
    finally:
        if apply:
            try:
                lease.release()
            except Exception as e:
                out(f'lease release failed: {e}')
    return 0


def main(argv=None, *, client=None, **inject):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stages', default=','.join(STAGES), help='fetch,judge,deliberate,requalify,publish のうち回す段(カンマ区切り)')
    ap.add_argument('--limit', type=int, default=None, help='全段共通の 1 回あたりの件数(段別指定が優先)')
    for s in STAGES:
        ap.add_argument(f'--{s}-limit', type=int, default=None, help=f'{s} 段の 1 回あたりの件数(既定 {DEFAULT_LIMITS[s]})')
    ap.add_argument('--rounds', type=int, default=0, help='回数。0 = 残りが無くなるか停止条件まで')
    ap.add_argument('--max-seconds', type=float, default=None, help='この秒数を超えたら次の回に入らず終了')
    ap.add_argument('--budget-month', type=float, default=judge_stage.MONTHLY_CAP_USD, help='月の LLM 費用の上限(USD)')
    ap.add_argument('--max-publish-per-day', type=int, default=MAX_PUBLISH_PER_DAY, help=f'直近 24 時間に作られた sites の日次上限(1 以上。既定 {MAX_PUBLISH_PER_DAY})')
    ap.add_argument('--judge-concurrency', type=int, default=1, help='判定段の並列数(1〜4)')
    ap.add_argument('--priority', choices=prio.MODES, default='leaf', help='取得・判定の行の選び方。leaf = 掲載数が目標未満の葉を埋める行を先に(機械順。既定)、none = identity_key 順')
    ap.add_argument('--backfill-empty', action='store_true', help='1 回だけ実行して終了: 判定済み(candidate/pending)で本文がほぼ空(text_len<80)・説明なしの行を empty_content で却下する(LLM 無し。既定 dry-run は件数だけ)')
    ap.add_argument('--reopen-empty', action='store_true', help='1 回だけ実行して終了: next_fetch_at が過ぎた empty_content の行を取得段へ戻す(next_fetch_at は印にすぎず自動では再開しない。既定 dry-run は件数だけ)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='既定。何も書かない')
    mode.add_argument('--apply', action='store_true', help='本番 D1 へ書く')
    a = ap.parse_args(argv)
    stages = [s.strip() for s in a.stages.split(',') if s.strip()]
    if not stages or any(s not in STAGES for s in stages):
        ap.error(f'--stages は {",".join(STAGES)} のカンマ区切り')
    limits = {}
    for s in STAGES:
        v = getattr(a, f'{s}_limit')
        v = a.limit if v is None else v
        limits[s] = DEFAULT_LIMITS[s] if v is None else v
    if min(limits.values()) < 1 or max(limits.values()) > MAX_LIMIT or a.rounds < 0 or (a.max_seconds is not None and a.max_seconds <= 0) or not 1 <= a.judge_concurrency <= 4 or a.budget_month <= 0 or a.max_publish_per_day < 1:
        ap.error(f'--limit は 1〜{MAX_LIMIT}、--rounds は 0 以上、--max-seconds は正、--judge-concurrency は 1〜4、--budget-month は正、--max-publish-per-day は 1 以上')
    if client is None:
        import media_online
        client = media_online.Cloudflare(min_request_interval=REQUEST_INTERVAL)
    if a.backfill_empty and a.reopen_empty:
        ap.error('--backfill-empty と --reopen-empty は同時に指定できない')
    if a.backfill_empty or a.reopen_empty:
        return one_shot(judge_stage.backfill_empty_content if a.backfill_empty else judge_stage.reopen_empty_content, client, a.apply,
                        lease=inject.get('lease'), out=inject.get('out', print))
    code, _, _ = run_pipeline(client, stages=stages, limits=limits, rounds=a.rounds, max_seconds=a.max_seconds, apply=a.apply,
                              budget_month=a.budget_month, concurrency=a.judge_concurrency, max_publish_per_day=a.max_publish_per_day, priority=a.priority, **inject)
    return code


if __name__ == '__main__':
    sys.exit(main())
