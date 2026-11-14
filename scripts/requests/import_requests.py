#!/usr/bin/env python3
"""受け付けた申請(D1 requests の pending)を、手元で巡回の候補に取り込む。Issue #90(#4, #29)。

  uv run python3 scripts/requests/import_requests.py [--local|--remote] [--emit-sql]

- 読み取り専用: D1 へは SELECT しか流さない。status の更新は --emit-sql が出す SQL を人が実行する。
- add     -> 発見パイプラインの frontier 台帳へ追記(source='request'。キーはホスト+パスの比較キー)。以降は既存の自動判定(robots・決定論フィルタ・LLM 判定)に任せ、人の承認は挟まない。
- remove / correct -> 人が見る候補ファイル(既定 var/requests/review.jsonl)へ理由つきで出す。`recrawl.py candidates` が表示する。
  申請だけでは掲載状態を変えない。
- 重複(frontier・掲載サイト(D1 の sites)に既にある推薦)は台帳に積まず approved 扱い(note=duplicate)、拒否リスト(blocked_hosts)に当たる推薦は
  rejected(note=blocklisted)。それ以外の取り込み済みは approved。冪等(同じ申請は二重に積まない)。
"""
import argparse
import glob
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'pipeline'))
import frontier  # noqa: E402

DEFAULT_REVIEW = ROOT / 'var' / 'requests' / 'review.jsonl'
PENDING_SQL = "SELECT id, type, url, host, normalized_key, reason, created_at FROM requests WHERE status='pending' ORDER BY id"
BLOCKED_SQL = "SELECT host, path_prefix FROM blocked_hosts"
SITES_SQL = "SELECT id, url FROM sites"


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


# ---- 照合（src/intake.js の normalizeUrl / matchPath / blockCandidates / isBlocked と同じ比較） ----

def _shared_suffixes():
    """共有ホスティングの一覧。正本は src/intake.js の SHARED_SUFFIXES（重複管理しない）。"""
    src = (ROOT / 'src' / 'intake.js').read_text(encoding='utf-8')
    m = re.search(r'SHARED_SUFFIXES\s*=\s*\[(.*?)\]', src, re.S)
    return re.findall(r"'([^']+)'", m.group(1)) if m else []


def _decode_unreserved(s):
    def f(m):
        c = chr(int(m.group(1), 16))
        return c if re.match(r'[A-Za-z0-9\-._~]', c) else '%' + m.group(1).upper()
    return re.sub(r'%([0-9a-fA-F]{2})', f, s)


def match_path(path):
    """パスの比較形（intake.js の matchPath）: %XX 復号、; 以降の除去、// の畳み込み、./.. の解決、末尾の . 空白 / を除く、小文字。"""
    d = _decode_unreserved(path)
    d = re.sub(r'%2f', '/', d, flags=re.I).replace('%20', ' ')
    d = re.sub(r'%5c', '/', d, flags=re.I)
    out = []
    for seg in d.split('/'):
        seg = re.sub(r';.*$', '', seg)
        if seg in ('', '.'):
            continue
        if seg == '..':
            if out:
                out.pop()
            continue
        out.append(seg)
    p = ('/' + '/'.join(out)) if out else ''
    return re.sub(r'[.\s/]+$', '', p).lower()


def key_of(url):
    """比較キー（スキーム・www.・末尾スラッシュ・大文字小文字・%7E と ~ を同一視）。申請の normalized_key と同じ形。"""
    u = url.strip()
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', u):
        u = 'https://' + u
    sp = urllib.parse.urlsplit(u)
    host = (sp.hostname or '').rstrip('.').lower()
    host = host[4:] if host.startswith('www.') else host
    return host + match_path(sp.path.rstrip('/'))


def listed_index(site_rows=(), files=None):
    """掲載済みサイトの {比較キー: site_id}。正本は D1 の sites（site_rows = [{id,url}]）。data/collection-*.json は補助。"""
    by_key = {}
    for row in site_rows:
        if row.get('id') and row.get('url'):
            by_key.setdefault(key_of(row['url']), row['id'])
    for f in files if files is not None else sorted(glob.glob(str(ROOT / 'data' / 'collection-*.json'))):
        for row in json.loads(Path(f).read_text(encoding='utf-8')):
            if row.get('id') and row.get('url'):
                by_key.setdefault(key_of(row['url']), row['id'])
    return by_key


def block_candidates(host, shared=None):
    """拒否リスト照合用の候補ホスト（host 自身と親ドメイン。共有ホスティングはユーザー領域までで止める）。"""
    shared = _shared_suffixes() if shared is None else shared
    h = host[4:] if host.startswith('www.') else host
    labels = h.split('.')
    sh = next((x for x in shared if h == x or h.endswith('.' + x)), None)
    mn = len(sh.split('.')) + 1 if sh else 2
    return [('.'.join(labels[i:])) for i in range(len(labels)) if len(labels) - i >= mn]


def is_blocked(norm_key, blocked, shared=None):
    """blocked_hosts の行(host, path_prefix)に当たるか（intake.js の isBlocked と同じ）。"""
    host, _, rest = norm_key.partition('/')
    path = '/' + rest if rest else ''
    cands = set(block_candidates(host, shared))
    for b in blocked:
        bh, pp = b['host'], b.get('path_prefix') or ''
        if pp:
            if bh == host and (path == pp or path.startswith(pp + '/')):
                return True
        elif bh in cands:
            return True
    return False


# ---- 取り込み ----

def _read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for l in p.read_text(encoding='utf-8').splitlines():
        try:
            if l.strip():
                out.append(json.loads(l))
        except ValueError:
            continue   # 壊れた行は読み飛ばす
    return out


def _review_ids(path):
    return {e['request_id'] for e in _read_jsonl(path) if isinstance(e, dict) and 'request_id' in e}


def reviewed_path(review_path):
    return Path(str(review_path) + '.reviewed')


def mark_reviewed(review_path, ids):
    """人が見終えた申請を「済み」にする（candidates に出なくなる。review.jsonl 自体は消さない）。"""
    p = reviewed_path(review_path); p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as f:
        for i in ids:
            f.write(f'{int(i)}\n')


def import_requests(rows, db, review_path, blocked=(), listed=None):
    """pending の行を振り分ける。戻り値は {id: (status, note)}（SQL 生成用）と件数。
    listed は listed_index() の戻り値（比較キー -> site_id。テスト用に差し替え可）。"""
    listed = listed_index() if listed is None else listed
    seen = _review_ids(review_path)
    decisions, counts = {}, {'frontier': 0, 'review': 0, 'duplicate': 0, 'blocklisted': 0, 'skipped': 0}
    new_review = []
    for r in rows:
        rid, typ = r['id'], r['type']
        nk = r['normalized_key'] or key_of(r['url'])
        if typ == 'add':
            if is_blocked(nk, blocked):
                decisions[rid] = ('rejected', 'blocklisted'); counts['blocklisted'] += 1; continue
            if nk in listed:
                decisions[rid] = ('approved', 'duplicate'); counts['duplicate'] += 1; continue
            # キーはホスト+パス（パス型のユーザー領域 ameblo.jp/bar や asahi-net.or.jp/~x を別サイトとして扱う）
            added = frontier.add(db, nk, r['host'], r['url'], 'request', source_detail=f'request#{rid}')
            if added:
                same = True
            else:   # SQL 未適用のまま再実行した場合、自分が積んだ行は imported のまま
                ex = db.execute('SELECT source_detail FROM frontier WHERE key=?', (nk,)).fetchone()
                same = bool(ex) and ex['source_detail'] == f'request#{rid}'
            decisions[rid] = ('approved', 'imported' if same else 'duplicate')
            counts['frontier' if same else 'duplicate'] += 1
        elif typ in ('remove', 'correct'):
            if rid not in seen:
                new_review.append({'request_id': rid, 'type': typ, 'url': r['url'], 'site_id': listed.get(nk),
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
    ap.add_argument('--mark-reviewed', nargs='+', type=int, metavar='ID', help='人が見終えた申請(remove/correct)を済みにする(D1 には触れない)')
    ap.add_argument('--emit-sql', action='store_true', help='status 更新 SQL を標準出力へ(D1 へは書かない)')
    a = ap.parse_args(argv)
    if a.mark_reviewed:
        mark_reviewed(a.review_file, a.mark_reviewed)
        print(f'{len(a.mark_reviewed)} 件を済みにした')
        return
    rows = d1_select(PENDING_SQL, a.remote, a.persist_to, runner)
    blocked = d1_select(BLOCKED_SQL, a.remote, a.persist_to, runner)
    listed = listed_index(d1_select(SITES_SQL, a.remote, a.persist_to, runner))   # 掲載サイトの正本は D1 の sites（読み取りのみ）
    db = frontier.connect(a.frontier_db)
    try:
        decisions, c = import_requests(rows, db, a.review_file, blocked, listed)
    finally:
        db.close()
    msg = f"pending {len(rows)} 件: frontier へ {c['frontier']}、人が見る候補へ {c['review']}、重複 {c['duplicate']}、拒否リスト該当 {c['blocklisted']}"
    print(msg, file=sys.stderr if a.emit_sql else sys.stdout)
    if a.emit_sql:
        print(emit_sql(decisions), end='')


if __name__ == '__main__':
    main()
