#!/usr/bin/env python3
"""独立サイトの発見パイプライン（決定論。LLM/エージェントのトークンは使わない）。

  uv run python3 scripts/pipeline/run.py init|discover-cc|discover-links|triage|fetch|judge|report [options]
"""
import argparse
import collections
import concurrent.futures
import datetime
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import discover
import features
import fetcher
import frontier
from hostnames import DEFERRED_REASONS, prefilter, registrable, subdomain_of_registrable

SHARED_SUBDOMAIN_THRESHOLD = 3        # 同じ登録ドメイン配下のサブドメインホストがこれ以上台帳にあれば共有サービスとみなして保留

MAX_ATTEMPTS = 3                      # failed の再試行上限（取得試行の通算）
BACKOFF_DAYS = 1                      # 再試行までの待ち: 1日 × 2^(試行-1)。1回目の失敗後 1 日、2回目の失敗後 2 日、3回目の失敗で打ち止め


def triage(db, redo=False):
    """取得前フィルタ: discovered -> queued | rejected | deferred（共有サービス配下の個人領域。基準の決定は別Issue）"""
    n = collections.Counter()
    if redo:  # 取得前に除外されたもの(未取得)を、ルール変更後に引き直す
        db.execute("UPDATE frontier SET state='discovered', reject_reason=NULL WHERE state='rejected' AND last_fetched_at IS NULL")
    if redo:
        db.execute("UPDATE frontier SET state='discovered', reject_reason=NULL WHERE state='deferred'")
    # 共有サービスの推定: 台帳に apex も www も無く(= 本体がリンクされていない)、配下のホストだけが多数ある登録ドメイン。
    # apex/www がある登録ドメイン、または links（既掲載サイトからのリンク）由来のホストを含む登録ドメインは、独立サイトの機能サブドメイン群とみなして対象外。
    keys = [k.split('/')[0] for (k,) in db.execute('SELECT key FROM frontier')]   # パス付きキー(request 由来のユーザー領域)はホスト部だけで数える
    # 免除の根拠にするのは発見元が申請(request)でない行だけ。申請者は外部の提出者で、パス付きの推薦(sharedsvc.jp/someone)は共有サービスの証拠でもあるため、
    # 提出によって同じドメイン配下の保留を外せないようにする（申請の URL 自体は通常どおり審査される）
    exempt = {registrable(k.split('/')[0]) for (k,) in db.execute("SELECT key FROM frontier WHERE source!='request'") if '/' not in k and not subdomain_of_registrable(k)}
    exempt |= {registrable(k.split('/')[0]) for (k,) in db.execute("SELECT key FROM frontier WHERE source='links'")}
    fans = collections.Counter(registrable(k) for k in keys if subdomain_of_registrable(k) and registrable(k) not in exempt)
    for r in db.execute("SELECT key, host, source FROM frontier WHERE state='discovered'").fetchall():
        reason = prefilter(r['host'], jp_only=r['source'] not in ('links', 'request'))   # 申請の推薦は .jp 以外も許す（国は掲載時の country_code が決める）
        if not reason and subdomain_of_registrable(r['host']) and fans[registrable(r['host'])] >= SHARED_SUBDOMAIN_THRESHOLD:
            reason = 'shared_service_user'   # 台帳内で同じドメイン配下のホストが多い = 共有サービスの利用者領域の可能性
        if reason in DEFERRED_REASONS:
            db.execute("UPDATE frontier SET state='deferred', reject_reason=? WHERE key=?", (reason, r['key']))
        elif reason:
            db.execute("UPDATE frontier SET state='rejected', reject_reason=? WHERE key=?", (reason, r['key']))
        else:
            db.execute("UPDATE frontier SET state='queued', next_fetch_at=? WHERE key=?", (frontier.now(), r['key']))
        n[reason or 'queued'] += 1
    db.commit()
    return n


def process(row):
    """1ホストの取得と特徴抽出（ネットワークのみ。DBには触らない）。"""
    res = fetcher.fetch_site(row['host'], path=urllib.parse.urlsplit(row['url']).path or '/')   # 申請のパス付き推薦は、そのパスを取る（それ以外は '/'）
    out = {'key': row['key'], 'robots_status': res['robots_status'], 'status': res['status'], 'final_url': res['final_url'], 'error': res['error'], 'feat': None}
    if not res['error'] and res['status'] == 200:
        ctype = res['headers'].get('content-type', '').lower()
        if 'html' in ctype or not ctype:
            out['feat'] = features.extract(row['host'], fetcher.decode_body(res['body'], res['headers']), res['headers'])
        else:
            out['error'] = 'not_html'
    return out


def fetch(db, limit, workers):
    # 新規(queued)と、バックオフが明けた failed（試行 MAX_ATTEMPTS 未満）を対象にする
    rows = db.execute("SELECT key, host, url FROM frontier WHERE (state='queued' OR (state='failed' AND fetch_attempts<?)) AND (next_fetch_at IS NULL OR next_fetch_at<=?) ORDER BY state DESC, priority DESC, key LIMIT ?", (MAX_ATTEMPTS, frontier.now(), limit)).fetchall()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(process, r): r for r in rows}
        for fut in concurrent.futures.as_completed(futs):
            try:
                o = fut.result()
            except Exception as e:  # 想定外は failed にして続行
                o = {'key': futs[fut]['key'], 'robots_status': None, 'status': None, 'final_url': None, 'error': f'exception:{type(e).__name__}', 'feat': None}
            apply_result(db, o); done += 1
            if done % 25 == 0:
                db.commit(); print(f'  fetched {done}/{len(rows)}', file=sys.stderr)
    db.commit()
    return done


def apply_result(db, o):
    err = o['error']; now = frontier.now()
    sets = {'last_fetched_at': now, 'last_http_status': o['status'], 'robots_status': o['robots_status'], 'final_url': o['final_url']}
    state, reason = 'fetched', None
    if err == 'robots_disallowed':
        state, reason = 'blocked_by_robots', 'robots_disallowed'
    elif err in ('redirect_offsite', 'ssrf_blocked', 'bad_port', 'bad_url', 'not_html'):
        state, reason = 'rejected', err
    elif err in ('dns', 'connect', 'protocol', 'robots_unavailable', 'too_many_redirects') or (err or '').startswith('exception'):
        state, reason = 'failed', err
    elif o['feat'] is not None:
        sets.update(o['feat'])
    else:
        h = features.judge_http(o['status'])
        if h: state, reason = h
    if state == 'failed':
        sets['last_error'] = reason
        attempts = db.execute('SELECT fetch_attempts FROM frontier WHERE key=?', (o['key'],)).fetchone()[0] + 1
        wait = datetime.timedelta(days=BACKOFF_DAYS * 2 ** (attempts - 1))
        sets['next_fetch_at'] = (datetime.datetime.now(datetime.timezone.utc) + wait).isoformat(timespec='seconds')
    if state != 'failed':
        sets['last_error'] = None   # 再取得に成功/確定したら前回のエラーを消す
    sets.update(state=state, reject_reason=reason)
    cols = ', '.join(f'{k}=?' for k in sets)
    db.execute(f'UPDATE frontier SET {cols}, fetch_attempts=fetch_attempts+1 WHERE key=?', (*sets.values(), o['key']))


def judge(db):
    """取得後フィルタ: fetched -> candidate | rejected（再実行可能。閾値を変えたら再判定できる）"""
    n = collections.Counter()
    for r in db.execute("SELECT * FROM frontier WHERE state IN ('fetched','candidate') OR (state='rejected' AND title IS NOT NULL AND last_http_status=200)").fetchall():
        st, reason = features.judge(r)
        adult = features.adult_attr(r)
        # 成人向け(yes)は本文冒頭と description を保存しない（title と属性だけ残す）
        db.execute('UPDATE frontier SET state=?, reject_reason=?, site_kind=?, adult=?, text_head=CASE WHEN ?=\'yes\' THEN NULL ELSE text_head END, description=CASE WHEN ?=\'yes\' THEN NULL ELSE description END WHERE key=?', (st, reason, features.site_kind(r['host'], r['title'], r['description']), adult, adult, adult, r['key']))
        n[reason or 'candidate'] += 1
    db.commit()
    return n


def report(db, sample):
    print('== state'); [print(f'{r[0]:20} {r[1]}') for r in db.execute('SELECT state, count(*) FROM frontier GROUP BY 1 ORDER BY 2 DESC')]
    print('== state / reason'); [print(f'{r[0]:20} {r[1] or "-":28} {r[2]}') for r in db.execute('SELECT state, reject_reason, count(*) FROM frontier GROUP BY 1,2 ORDER BY 3 DESC')]
    print('== site_kind (candidate)'); [print(f'{r[0] or "-":8} {r[1]}') for r in db.execute("SELECT site_kind, count(*) FROM frontier WHERE state='candidate' GROUP BY 1 ORDER BY 2 DESC")]
    print('== adult (candidate)'); [print(f'{r[0] or "-":8} {r[1]}') for r in db.execute("SELECT adult, count(*) FROM frontier WHERE state='candidate' GROUP BY 1")]
    print('== source'); [print(f'{r[0]:24} {r[1]}') for r in db.execute('SELECT source, count(*) FROM frontier GROUP BY 1')]
    print(f'== candidate sample (host 順に等間隔で {sample} 件。偏りを避けるため score 順は使わない)')
    rows = db.execute("SELECT host, title, description, personal_score, site_kind, adult, ad_hits, text_len FROM frontier WHERE state='candidate' ORDER BY key").fetchall()
    step = max(1, len(rows) // max(sample, 1))
    for r in rows[::step][:sample]:
        title = (r['title'] or '')[:50]; desc = re.sub(r'\s+', ' ', r['description'] or '')[:60]
        print(f'{r["host"]} | {title} | {desc} | kind={r["site_kind"]} adult={r["adult"]} ads={r["ad_hits"]} len={r["text_len"]}')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('cmd', choices=['init', 'discover-cc', 'discover-links', 'triage', 'fetch', 'judge', 'report'])
    ap.add_argument('--db', default=None)
    ap.add_argument('--part-file', help='discover-cc: ローカルの vertices パート (.gz)。省略時は自動取得（約40MB）')
    ap.add_argument('--sample-every', type=int, default=1000, help='discover-cc: ホスト名のハッシュで 1/N を決定論的に抽出')
    ap.add_argument('--limit', type=int, default=100, help='discover-cc: 追加上限 / fetch: 取得ホスト数上限')
    ap.add_argument('--workers', type=int, default=3, help='fetch: 並列数（2〜4。ホストは重複しない）')
    ap.add_argument('--redo', action='store_true', help='triage: 未取得の rejected を引き直す')
    ap.add_argument('--sample', type=int, default=20, help='report: 候補サンプル件数')
    a = ap.parse_args()
    if not 1 <= a.workers <= 4:
        ap.error('--workers は 1〜4')
    db = frontier.connect(a.db)
    if a.cmd == 'discover-cc': print(discover.discover_commoncrawl(db, a.part_file, a.sample_every, a.limit))
    elif a.cmd == 'discover-links': print(discover.discover_links(db))
    elif a.cmd == 'triage': print(dict(triage(db, a.redo)))
    elif a.cmd == 'fetch': print('fetched', fetch(db, a.limit, a.workers))
    elif a.cmd == 'judge': print(dict(judge(db)))
    elif a.cmd == 'report': report(db, a.sample)


if __name__ == '__main__':
    main()
