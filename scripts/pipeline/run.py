#!/usr/bin/env python3
"""個人・小規模サイト発見パイプライン（決定論。LLM/エージェントのトークンは使わない）。

  uv run python3 scripts/pipeline/run.py init|discover-cc|discover-links|triage|fetch|judge|report [options]
"""
import argparse
import collections
import concurrent.futures
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import discover
import features
import fetcher
import frontier
from hostnames import prefilter

RETRY_AFTER = datetime.timedelta(days=1)


def triage(db):
    """取得前フィルタ: discovered -> queued | rejected"""
    n = collections.Counter()
    for r in db.execute("SELECT key, host, source FROM frontier WHERE state='discovered'").fetchall():
        reason = prefilter(r['host'], jp_only=r['source'] != 'links')
        if reason:
            db.execute("UPDATE frontier SET state='rejected', reject_reason=? WHERE key=?", (reason, r['key']))
        else:
            db.execute("UPDATE frontier SET state='queued', next_fetch_at=? WHERE key=?", (frontier.now(), r['key']))
        n[reason or 'queued'] += 1
    db.commit()
    return n


def process(row):
    """1ホストの取得と特徴抽出（ネットワークのみ。DBには触らない）。"""
    res = fetcher.fetch_site(row['host'])
    out = {'key': row['key'], 'robots_status': res['robots_status'], 'status': res['status'], 'final_url': res['final_url'], 'error': res['error'], 'feat': None}
    if not res['error'] and res['status'] == 200:
        ctype = res['headers'].get('content-type', '').lower()
        if 'html' in ctype or not ctype:
            out['feat'] = features.extract(row['host'], fetcher.decode_body(res['body'], res['headers']), res['headers'])
        else:
            out['error'] = 'not_html'
    return out


def fetch(db, limit, workers):
    rows = db.execute("SELECT key, host FROM frontier WHERE state='queued' AND (next_fetch_at IS NULL OR next_fetch_at<=?) ORDER BY priority DESC, key LIMIT ?", (frontier.now(), limit)).fetchall()
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
        sets['next_fetch_at'] = (datetime.datetime.now(datetime.timezone.utc) + RETRY_AFTER).isoformat(timespec='seconds')
    elif o['feat'] is not None:
        sets.update(o['feat'])
    else:
        h = features.judge_http(o['status'])
        if h: state, reason = h
    if state == 'failed':
        sets['last_error'] = reason
    sets.update(state=state, reject_reason=reason)
    cols = ', '.join(f'{k}=?' for k in sets)
    db.execute(f'UPDATE frontier SET {cols}, fetch_attempts=fetch_attempts+1 WHERE key=?', (*sets.values(), o['key']))


def judge(db):
    """取得後フィルタ: fetched -> candidate | rejected（再実行可能。閾値を変えたら再判定できる）"""
    n = collections.Counter()
    for r in db.execute("SELECT * FROM frontier WHERE state IN ('fetched','candidate') OR (state='rejected' AND title IS NOT NULL AND last_http_status=200)").fetchall():
        st, reason = features.judge(r)
        db.execute('UPDATE frontier SET state=?, reject_reason=? WHERE key=?', (st, reason, r['key']))
        n[reason or 'candidate'] += 1
    db.commit()
    return n


def report(db, sample):
    print('== state'); [print(f'{r[0]:20} {r[1]}') for r in db.execute('SELECT state, count(*) FROM frontier GROUP BY 1 ORDER BY 2 DESC')]
    print('== state / reason'); [print(f'{r[0]:20} {r[1] or "-":28} {r[2]}') for r in db.execute('SELECT state, reject_reason, count(*) FROM frontier GROUP BY 1,2 ORDER BY 3 DESC')]
    print('== source'); [print(f'{r[0]:24} {r[1]}') for r in db.execute('SELECT source, count(*) FROM frontier GROUP BY 1')]
    print(f'== candidate sample (personal_score desc, {sample})')
    for r in db.execute("SELECT host, title, description, personal_score, ad_hits, text_len FROM frontier WHERE state='candidate' ORDER BY personal_score DESC, key LIMIT ?", (sample,)):
        print(f'{r["host"]} | {r["title"][:50]} | {(r["description"] or "")[:60]} | score={r["personal_score"]} ads={r["ad_hits"]} len={r["text_len"]}')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('cmd', choices=['init', 'discover-cc', 'discover-links', 'triage', 'fetch', 'judge', 'report'])
    ap.add_argument('--db', default=None)
    ap.add_argument('--part-file', help='discover-cc: ローカルの vertices パート (.gz)。省略時は自動取得（約40MB）')
    ap.add_argument('--sample-every', type=int, default=1000, help='discover-cc: ホスト名のハッシュで 1/N を決定論的に抽出')
    ap.add_argument('--limit', type=int, default=100, help='discover-cc: 追加上限 / fetch: 取得ホスト数上限')
    ap.add_argument('--workers', type=int, default=3, help='fetch: 並列数（2〜4。ホストは重複しない）')
    ap.add_argument('--sample', type=int, default=20, help='report: 候補サンプル件数')
    a = ap.parse_args()
    if not 1 <= a.workers <= 4:
        ap.error('--workers は 1〜4')
    db = frontier.connect(a.db)
    if a.cmd == 'discover-cc': print(discover.discover_commoncrawl(db, a.part_file, a.sample_every, a.limit))
    elif a.cmd == 'discover-links': print(discover.discover_links(db))
    elif a.cmd == 'triage': print(dict(triage(db)))
    elif a.cmd == 'fetch': print('fetched', fetch(db, a.limit, a.workers))
    elif a.cmd == 'judge': print(dict(judge(db)))
    elif a.cmd == 'report': report(db, a.sample)


if __name__ == '__main__':
    main()
