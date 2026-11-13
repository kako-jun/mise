#!/usr/bin/env python3
"""受け付けた申請(D1 requests の pending)を、手元で巡回の候補に取り込む。Issue #90(#4, #29)。

  uv run python3 scripts/requests/import_requests.py [--local|--remote] [--emit-sql]

- 読み取り専用: D1 へは SELECT しか流さない。status の更新は --emit-sql が出す SQL を人が実行する。
- add     -> 発見パイプラインの frontier 台帳へ追記(source='request')。以降は既存の自動判定(robots・決定論フィルタ・LLM 判定)に任せ、人の承認は挟まない。
- remove / correct -> 人が見る候補ファイル(既定 var/requests/review.jsonl)へ理由つきで出す。`recrawl.py candidates` が表示する。
  申請だけでは掲載状態を変えない。
- 重複(frontier・掲載サイトに既にある推薦)は台帳に積まず approved 扱い(note=duplicate)、拒否リスト(blocked_hosts)に当たる推薦は
  rejected(note=blocklisted)。それ以外の取り込み済みは approved。冪等(同じ申請は二重に積まない)。
"""
import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
import frontier  # noqa: E402
from hostnames import host_key  # noqa: E402

DEFAULT_REVIEW = ROOT / 'var' / 'requests' / 'review.jsonl'
PENDING_SQL = "SELECT id, type, url, host, normalized_key, reason, created_at FROM requests WHERE status='pending' ORDER BY id"
BLOCKED_SQL = "SELECT host, path_prefix FROM blocked_hosts"


# ---- D1 読み取り(wrangler) ----

def d1_select(sql, remote=False, persist_to=None, runner=subprocess.run):
    """wrangler d1 execute --json で SELECT を実行し、行のリストを返す。"""
    cmd = ['npx', 'wrangler', 'd1', 'execute', 'mise', '--remote' if remote else '--local', '--json', '--command', sql]
    if persist_to and not remote:
        cmd += ['--persist-to', str(persist_to)]
    p = runner(cmd, cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f'wrangler が失敗: {p.stderr.strip()[-500:]}')
    out = p.stdout
    data = json.loads(out[out.index('['):])
    rows = []
    for part in data:
        if part.get('success') is False:
            raise RuntimeError(f'D1 のエラー: {part}')
        rows += part.get('results') or []
    return rows


# ---- 照合 ----

def key_of(url):
    """申請の normalized_key と同じ流儀の比較キー(スキーム・www.・末尾スラッシュ・大文字小文字を同一視)。"""
    u = url.strip()
    for s in ('https://', 'http://'):
        if u.lower().startswith(s):
            u = u[len(s):]
    u = u.split('?')[0].split('#')[0].rstrip('/')
    u = u.lower()
    return u[4:] if u.startswith('www.') else u


def listed_index(files=None):
    """掲載済みサイト(data/collection-*.json)の {比較キー: site_id} と {ホストキー: site_id}。"""
    by_key, by_host = {}, {}
    for f in files if files is not None else sorted(glob.glob(str(ROOT / 'data' / 'collection-*.json'))):
        for row in json.loads(Path(f).read_text(encoding='utf-8')):
            if row.get('id') and row.get('url'):
                k = key_of(row['url'])
                by_key.setdefault(k, row['id'])
                by_host.setdefault(k.split('/')[0], row['id'])
    return by_key, by_host


def is_blocked(norm_key, blocked):
    """blocked_hosts の行(host, path_prefix)に当たるか。ホスト全体の行は登録ドメイン配下も、パス型は同一ホストのそのパス配下だけ。"""
    host, _, rest = norm_key.partition('/')
    path = '/' + rest if rest else ''
    for b in blocked:
        bh, pp = b['host'], b.get('path_prefix') or ''
        if pp:
            if host == bh and (path == pp or path.startswith(pp + '/')):
                return True
        elif host == bh or host.endswith('.' + bh):
            return True
    return False


# ---- 取り込み ----

def _review_ids(path):
    p = Path(path)
    if not p.exists():
        return set()
    return {json.loads(l)['request_id'] for l in p.read_text(encoding='utf-8').splitlines() if l.strip()}


def import_requests(rows, db, review_path, blocked=(), listed=None):
    """pending の行を振り分ける。戻り値は {id: (status, note)}(SQL 生成用)と件数。
    listed は listed_index() の戻り値(テスト用に差し替え可)。"""
    by_key, by_host = listed if listed is not None else listed_index()
    seen = _review_ids(review_path)
    decisions, counts = {}, {'frontier': 0, 'review': 0, 'duplicate': 0, 'blocklisted': 0, 'skipped': 0}
    new_review = []
    for r in rows:
        rid, typ = r['id'], r['type']
        nk = r['normalized_key'] or key_of(r['url'])
        if typ == 'add':
            if is_blocked(nk, blocked):
                decisions[rid] = ('rejected', 'blocklisted'); counts['blocklisted'] += 1; continue
            hk = host_key(r['host'])
            if hk in by_host:
                decisions[rid] = ('approved', 'duplicate'); counts['duplicate'] += 1; continue
            added = frontier.add(db, hk, r['host'], r['url'], 'request', source_detail=f'request#{rid}')
            decisions[rid] = ('approved', 'imported' if added else 'duplicate')
            counts['frontier' if added else 'duplicate'] += 1
        elif typ in ('remove', 'correct'):
            if rid not in seen:
                site_id = by_key.get(nk) or by_host.get(nk.split('/')[0])
                new_review.append({'request_id': rid, 'type': typ, 'url': r['url'], 'site_id': site_id,
                                   'reason': r['reason'], 'created_at': r['created_at'],
                                   'state': f'request_{typ}', 'note': '申請の理由(利用者の入力。プレーンテキストとして扱う)'})
                seen.add(rid)
            decisions[rid] = ('approved', 'imported'); counts['review'] += 1
        else:
            counts['skipped'] += 1
    db.commit()
    if new_review:
        p = Path(review_path); p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('a', encoding='utf-8') as f:
            for e in new_review:
                f.write(json.dumps(e, ensure_ascii=False) + '\n')
    return decisions, counts


def _q(v):
    return "'" + str(v).replace("'", "''") + "'"


def emit_sql(decisions):
    """status を更新する SQL(pending のものだけ。人が wrangler d1 execute で実行する)。"""
    lines = ['-- generated by scripts/requests/import_requests.py --emit-sql (Issue #90)']
    for rid, (status, note) in sorted(decisions.items()):
        lines.append(f"UPDATE requests SET status={_q(status)}, note={_q(note)}, decided_at=datetime('now') WHERE id={int(rid)} AND status='pending';")
    return '\n'.join(lines) + '\n'


def main(argv=None, runner=subprocess.run):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--local', action='store_true', help='ローカル D1 から読む(既定)')
    g.add_argument('--remote', action='store_true', help='本番 D1 から読む(読み取りのみ。明示指定のときだけ)')
    ap.add_argument('--persist-to', help='--local の D1 保存先(wrangler の --persist-to)')
    ap.add_argument('--frontier-db', default=None, help=f'frontier 台帳(既定 {frontier.DEFAULT_DB})')
    ap.add_argument('--review-file', default=str(DEFAULT_REVIEW), help='remove/correct の出力先(JSON Lines)')
    ap.add_argument('--emit-sql', action='store_true', help='status 更新 SQL を標準出力へ(D1 へは書かない)')
    a = ap.parse_args(argv)
    rows = d1_select(PENDING_SQL, a.remote, a.persist_to, runner)
    blocked = d1_select(BLOCKED_SQL, a.remote, a.persist_to, runner)
    db = frontier.connect(a.frontier_db)
    try:
        decisions, c = import_requests(rows, db, a.review_file, blocked)
    finally:
        db.close()
    msg = f"pending {len(rows)} 件: frontier へ {c['frontier']}、人が見る候補へ {c['review']}、重複 {c['duplicate']}、拒否リスト該当 {c['blocklisted']}"
    print(msg, file=sys.stderr if a.emit_sql else sys.stdout)
    if a.emit_sql:
        print(emit_sql(decisions), end='')


if __name__ == '__main__':
    main()
