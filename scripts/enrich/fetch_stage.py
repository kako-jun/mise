"""取得段(オンライン補強パイプライン A): url_progress の discovered 行を取得し、特徴を url_candidates へ書く。

正本は本番 D1 だけ。ローカルファイル・キャッシュは一切作らない(メモリのみ)。取得・特徴抽出は scripts/pipeline の
fetcher / features / hostnames をそのまま使う(SSRF 対策・robots 遵守・512KB 上限・20 秒・オフサイトのリダイレクト拒否)。

状態遷移(url_progress.state):
  discovered -> queued(claim) -> fetched | blocked(robots) | rejected(入口/駐車/noindex 等) | failed(恒久エラー・5 回失敗)
                              -> discovered(一時エラー: fetch_attempts+1 と next_fetch_at のバックオフ)
成人向けは拒否せず fetch_adult 属性として残すだけ。人気・規模での選別や並べ替えはしない(identity_key の機械順)。
"""
import collections
import concurrent.futures
import datetime
import secrets
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'pipeline'))
import features
import fetcher
import freshness
from hostnames import host_key, platform_page, prefilter, priority

STALE_MINUTES = 30        # queued のまま止まった claim を再取得できるまでの時間
MAX_ATTEMPTS = 5          # 一時エラーでこの回数失敗したら failed
BACKOFF_HOURS = 1         # 一時エラー後の待ち: 1h * 2^(試行-1)
HOST_INTERVAL = 2.0       # 同一ホストへのリクエスト間隔の下限(秒)
WORKERS = 4
BATCH_STATEMENTS = 5      # client.batch 1 回の文の数(discover_online と同じ)
CLAIM_ROWS = 5            # 1 回に claim・取得・書き込みする行数(長い run でも claim が古びない)
WRITE_ROWS = 2            # 1 行 = 2 文(upsert + 状態更新)
PUBLIC_BODY_REASON = 'public_body_other_pipeline'   # 公的機関は別パイプライン(public-bodies)で扱う
TEXT_HEAD_MAX = 300       # features.extract の text_head 上限

PERMANENT_ERRORS = {'not_html'}                       # failed(恒久)
REJECT_ERRORS = {'redirect_offsite', 'ssrf_blocked', 'bad_port', 'bad_url'}   # rejected(取得対象外)
FMT = '%Y-%m-%d %H:%M:%S'



def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def host_of(url):
    return (urllib.parse.urlsplit(url).hostname or '').lower()


def dns_is_permanent(detail):
    """NXDOMAIN 相当(Name or service not known / no address)だけ恒久。SERVFAIL・一時的な失敗は再試行。"""
    d = (detail or '').lower()
    return 'not known' in d or 'no address' in d or 'nodata' in d or 'errno -2' in d or 'errno -5' in d


def fetch_one(url):
    """1 URL の取得と特徴抽出(ネットワークのみ・DB には触れない)。scripts/pipeline/run.py の process と同じ処理。"""
    host = host_of(url)
    path = urllib.parse.urlsplit(url).path or '/'
    res = fetcher.fetch_site(host, path=path)
    out = {'robots_status': res['robots_status'], 'status': res['status'], 'final_url': res['final_url'],
           'error': res['error'], 'error_detail': res.get('error_detail', ''), 'feat': None}
    if not res['error'] and res['status'] == 200:
        ctype = res['headers'].get('content-type', '').lower()
        if 'html' in ctype or not ctype:
            feat = features.extract(host, fetcher.decode_body(res['body'], res['headers']), res['headers'])
            footer = (feat['freshness_signal_at'], feat['freshness_source'])
            feat['freshness_signal_at'], feat['freshness_source'] = freshness.latest(list(res.get('freshness') or []) + [footer])
            out['feat'] = feat
        else:
            out['error'] = 'not_html'
    if out['error'] == 'dns':   # fetch_site は www/apex の最後のエラーだけ残すので、この行自身のホスト名が引けるかを確かめ直す
        try:
            fetcher.check_public_host(host)
        except fetcher.FetchError as e:
            out['dns_own'] = e.detail if e.code == 'dns' else None
    return out


def classify(out, host):
    """取得結果 -> (state, reason, transient)。state は url_progress の遷移先('discovered' は一時エラー)。"""
    err = out.get('error')
    if err == 'robots_disallowed':
        return 'blocked', 'robots', False
    if err in REJECT_ERRORS:
        return 'rejected', err, False
    if err in PERMANENT_ERRORS:
        return 'failed', err, False
    if err == 'dns':
        if dns_is_permanent(out.get('dns_own')):   # この行自身のホスト名が引けないときだけ恒久
            return 'failed', 'dns_nxdomain', False
        return 'discovered', 'dns', True
    if err:   # connect(タイムアウト含む) / protocol / robots_unavailable / too_many_redirects / exception:*
        return 'discovered', err, True
    status = out.get('status')
    if status is None:
        return 'discovered', 'no_response', True
    if status in (404, 410):
        return 'failed', f'http_{status}', False
    if status == 429 or status >= 500:
        return 'discovered', f'http_{status}', True
    if status == 403:
        return 'blocked', 'http_403', False
    if status != 200:
        return 'rejected', f'http_{status}', False
    feat = out.get('feat')
    if feat is None:
        return 'failed', 'not_html', False
    # 入口ページ・駐車・domain for sale・noindex は取得時点で確定(薄い・日本語でない等は判定段の仕事)
    row = {'flags': feat['flags'], 'title': feat['title'], 'text_len': 10 ** 9, 'kana_count': 10 ** 9, 'ad_hits': 0}
    st, reason = features.judge(row)
    if st == 'rejected' and reason in ('noindex', 'parking_or_default_page'):
        return 'rejected', reason, False
    return 'fetched', None, False


def pre_reject(host, url):
    """取得前に rejected にしてよいのは、無効なホスト・公的機関(別パイプライン)・サービスのトップ/サービス内ページだけ。
    共有サービス上の自作サイト(shared_service_user)や名前の形(spam_like_name)では落とさず、他のサイトと同じように取得する。"""
    reason = prefilter(host, jp_only=False)
    if reason == 'government':
        return PUBLIC_BODY_REASON
    if reason == 'invalid_host':
        return reason
    return platform_page(url)


FEATURE_COLS = ['title', 'description', 'lang', 'kana_count', 'text_len', 'ad_hits', 'ext_refs', 'flags', 'text_head',
                'personal_score', 'fetch_adult', 'fetch_site_kind', 'freshness_signal_at', 'freshness_source']


def candidate_params(row, host, out, state, reason, attempts, next_fetch_at, now):
    """url_candidates へ書く列。特徴列は抽出できたときだけ、取得系の列は実際に取得したときだけ含める(含めない列は上書きされない)。"""
    vals = {'host': host, 'url': row['url'], 'state': state, 'priority': priority(host), 'frontier_key': host_key(host),
            'reject_reason': reason if state in ('rejected', 'failed', 'blocked') else None}
    if out.get('pre'):
        return vals
    vals.update({'next_fetch_at': next_fetch_at, 'fetch_attempts': attempts, 'last_fetched_at': now, 'last_http_status': out.get('status'),
                 'last_error': reason if state in ('discovered', 'failed') else None,
                 'robots_status': out.get('robots_status'), 'final_url': out.get('final_url')})
    feat = out.get('feat')
    if feat:
        text_head, desc = feat.get('text_head'), feat.get('description')
        adult = features.adult_attr({'flags': feat['flags'], 'host': host})
        if adult == 'yes':   # 成人向けは本文冒頭と description を保存しない(title と属性だけ)。拒否はしない
            text_head = desc = None
        vals.update({k: feat.get(k) for k in FEATURE_COLS if k in feat}, description=desc,
                    text_head=text_head[:TEXT_HEAD_MAX] if text_head else None, fetch_adult=adult,
                    fetch_site_kind=features.site_kind(host, feat.get('title'), feat.get('description')))
    return vals


def upsert_sql(vals, row, claimed_at):
    """claim を持っている間だけ書く(claim を失った run は何も書かない)。競合時は vals の列だけ更新する。"""
    ins = {'identity_key': row['identity_key'], 'source': row['source'], 'discovered_at': row['discovered_at'], 'depth': 0, **vals}
    cols = list(ins)
    sql = (f"INSERT INTO url_candidates({','.join(cols)},updated_at) SELECT {','.join('?' * len(cols))},datetime('now') "
           f"WHERE EXISTS(SELECT 1 FROM url_progress WHERE identity_key=? AND state='queued' AND updated_at=?) "
           f"ON CONFLICT(identity_key) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in vals)},updated_at=datetime('now')")
    return sql, [ins[c] for c in cols] + [row['identity_key'], claimed_at]


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def claim(client, rows, now):
    """discovered(または 30 分超の queued)を queued にする。state と updated_at の比較交換で、2 台が同じ行を取らない。
    勝ったかどうかは RETURNING で判定する。row['claimed_at'] が書き込み時のガードになる。"""
    mine = []
    stamp = now.strftime(FMT)
    for r in rows:
        got = client.query("UPDATE url_progress SET state='queued', updated_at=? WHERE identity_key=? AND state=? AND updated_at=? RETURNING identity_key",
                           [stamp, r['identity_key'], r['state'], r['updated_at']])
        if got:
            r['claimed_at'] = stamp
            mine.append(r)
    return mine


def select_rows(client, limit, now):
    cutoff = (now - datetime.timedelta(minutes=STALE_MINUTES)).strftime(FMT)
    return client.query(
        "SELECT p.identity_key, p.url, p.source, p.state, p.discovered_at, p.updated_at, COALESCE(c.fetch_attempts,0) AS fetch_attempts "
        "FROM url_progress p LEFT JOIN url_candidates c ON c.identity_key=p.identity_key "
        "WHERE (p.state='discovered' AND (c.next_fetch_at IS NULL OR c.next_fetch_at<=?)) OR (p.state='queued' AND p.updated_at<?) "
        "ORDER BY p.identity_key LIMIT ?", [now.strftime(FMT), cutoff, int(limit)])


def _transient(e):
    return {'error': f'exception:{type(e).__name__}', 'status': None, 'robots_status': None, 'final_url': None, 'feat': None}


def process_chunk(rows, fetch, workers):
    """取得(ネットワークのみ)。同一ホストは順番に。戻り: [(row, host, out, state, reason)]。行ごとの例外は一時エラー。"""
    done, by_host = [], collections.defaultdict(list)
    for r in rows:
        host = host_of(r['url'])
        reason = pre_reject(host, r['url']) if host else 'invalid_host'
        if reason:
            done.append((r, host, {'pre': True, 'status': None, 'robots_status': None, 'final_url': None, 'feat': None}, 'rejected', reason))
        else:
            by_host[host].append(r)

    def one(r, host):
        try:
            out = fetch(r['url'])
            st, reason, _ = classify(out, host)
        except Exception as e:
            out = _transient(e)
            st, reason = 'discovered', out['error']
        return r, host, out, st, reason

    def run_host(host, group):
        return [one(r, host) for r in group]

    with concurrent.futures.ThreadPoolExecutor(max(1, workers)) as ex:
        for fut in [ex.submit(run_host, h, g) for h, g in by_host.items()]:
            done += fut.result()
    return done


def write_results(client, results, stamp, stats, log):
    for batch in chunks(results, WRITE_ROWS):
        stmts, expect = [], {}
        for r, host, out, st, reason in batch:
            attempts = int(r['fetch_attempts'] or 0)
            next_at = None
            if st == 'discovered':   # 一時エラー: 試行を数え、上限で failed
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    st = 'failed'
                else:
                    next_at = (stamp + datetime.timedelta(hours=BACKOFF_HOURS * 2 ** (attempts - 1))).strftime(FMT)
            elif not out.get('pre'):
                attempts += 1
            try:
                vals = candidate_params(r, host, out, st, reason, attempts, next_at, stamp.strftime(FMT))
            except Exception as e:   # 特徴の組み立てに失敗しても行を宙に浮かせない
                out = {**_transient(e)}
                attempts = int(r['fetch_attempts'] or 0) + 1
                st = 'failed' if attempts >= MAX_ATTEMPTS else 'discovered'
                reason = out['error']
                next_at = None if st == 'failed' else (stamp + datetime.timedelta(hours=BACKOFF_HOURS * 2 ** (attempts - 1))).strftime(FMT)
                vals = candidate_params(r, host, out, st, reason, attempts, next_at, stamp.strftime(FMT))
            stmts.append(upsert_sql(vals, r, r['claimed_at']))
            stmts.append(("UPDATE url_progress SET state=?, updated_at=? WHERE identity_key=? AND state='queued' AND updated_at=?",
                          [st, stamp.strftime(FMT), r['identity_key'], r['claimed_at']]))
            expect[r['identity_key']] = (st, None if out.get('pre') else attempts)
            stats[st if st != 'discovered' else 'retry'] += 1
        assert len(stmts) <= BATCH_STATEMENTS
        client.batch(stmts)
        keys = list(expect)
        back = {x['identity_key']: x for x in client.query(
            f"SELECT p.identity_key, p.state, c.fetch_attempts, c.state AS cstate FROM url_progress p LEFT JOIN url_candidates c ON c.identity_key=p.identity_key "
            f"WHERE p.identity_key IN ({','.join('?' * len(keys))})", keys)}
        for k, (st, attempts) in expect.items():
            b = back.get(k)
            if not b or b['state'] != st or b['cstate'] != st or (attempts is not None and b['fetch_attempts'] != attempts):
                stats['verify_failed'] += 1
                log(f'verify failed: {k} expected {st}/{attempts} got {dict(b) if b else None}')


def run(client, *, limit, apply, log=print, fetch=None, now=_utcnow, workers=WORKERS, heartbeat=None):
    """取得段の 1 回分。戻り: 件数の dict。apply=False は選択のみ(claim も書き込みもしない)。
    CLAIM_ROWS 行ずつ claim -> 取得 -> 書き込みまで進める(claim は書き込み直前まで新しい。claim を失った行は書かない)。"""
    fetch = fetch or fetch_one
    fetcher.HOST_WAIT = max(fetcher.HOST_WAIT, HOST_INTERVAL)
    rows = select_rows(client, limit, now())
    stats = collections.Counter(selected=len(rows))
    if not apply:
        log(f'dry-run: {len(rows)} 件が対象(claim・書き込みなし)')
        return dict(stats)
    for group in chunks(rows, CLAIM_ROWS):
        if heartbeat:
            heartbeat()   # raises when the runner lost its lease: stop before the next claim
        mine = claim(client, group, now())
        stats['claimed'] += len(mine)
        if mine:
            write_results(client, process_chunk(mine, fetch, workers), now(), stats, log)
    log('fetch stage: ' + ', '.join(f'{k}={v}' for k, v in sorted(stats.items())))
    return dict(stats)
