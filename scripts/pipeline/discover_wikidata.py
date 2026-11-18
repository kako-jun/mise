"""発見元: Wikidata の公式サイト(P856)を、葉ごとの対応表(data/wikidata-leaf-map.csv)で取る(#128)。

- 正本は対応表 1 つ。台帳には source='wikidata'、hint_leaf=対応表の葉 id で入れる(ヒントは判定を誘導しない弱い手がかり)。
- WDQS の利用ルール: UA に連絡先 URL、1 クエリずつ直列、ネットワークに出る呼び出しの間は WDQS_INTERVAL 秒以上あける、
  LIMIT/OFFSET のページング、クライアント側タイムアウト 60 秒、429/503 は Retry-After を尊重して中断、結果は var/wikidata-cache/ にキャッシュ。
- 対応表の行ごとに「次の OFFSET」を source_cursor に持ち、夜をまたいで続きから取る(同じ範囲を連打しない)。
- Wikidata のデータは CC0。画面には出さない(#111)。
"""
import csv
import datetime
import email.utils
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import fetcher
import frontier
from hostnames import host_key, normalize_host, priority

ROOT = frontier.ROOT
MAP_PATH = ROOT / 'data' / 'wikidata-leaf-map.csv'
CATEGORIES_PATH = ROOT / 'data' / 'categories.csv'
CACHE_DIR = ROOT / 'var' / 'wikidata-cache'
ENDPOINT = 'https://query.wikidata.org/sparql'
USER_AGENT = fetcher.USER_AGENT          # MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)
WDQS_INTERVAL = 5.0                      # ネットワークに出る呼び出しの最小間隔(秒)
TIMEOUT = 60                             # クライアント側のタイムアウト(WDQS のサーバー側も 60 秒)
PAGE_SIZE = 200
CACHE_TTL_DAYS = 30
SOURCE = 'wikidata'
MAP_COLUMNS = ['leaf_id', 'qid', 'rel', 'country_prop', 'subclass', 'label_en', 'note', 'max_rows']
QID = re.compile(r'^Q[1-9]\d{0,9}$')
PROP = re.compile(r'^P[1-9]\d{0,5}(\|P[1-9]\d{0,5})*$')


class WdqsAbort(Exception):
    """429/503 など、これ以上叩いてはいけないとき。retry_after は秒(不明なら None)。"""
    def __init__(self, status, retry_after=None):
        super().__init__(f'WDQS {status} retry_after={retry_after}')
        self.status, self.retry_after = status, retry_after


def parse_retry_after(value):
    """Retry-After(秒数または HTTP 日付) -> 秒。読めなければ None。"""
    v = (value or '').strip()
    if v.isdigit():
        return int(v)
    try:
        dt = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return max(0, int((dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()))


class WdqsSkip(Exception):
    """この 1 クエリだけ諦める(タイムアウトなど)。他の行は続ける。"""


# ------------------------------------------------------------------ 対応表
def load_map(path=MAP_PATH):
    with open(path, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def validate_map(rows, categories_path=CATEGORIES_PATH):
    """問題の一覧を返す(空なら正常)。葉 id は categories.csv の assign=llm の葉(子を持たない)だけ。"""
    with open(categories_path, encoding='utf-8', newline='') as f:
        cats = list(csv.DictReader(f))
    parents = {c['parent_id'] for c in cats}
    ok_leaf = {c['id'] for c in cats if c['assign'] == 'llm' and c['id'] not in parents}
    errs, seen = [], set()
    for i, r in enumerate(rows, 2):
        where = f'行{i}'
        if list(r.keys()) != MAP_COLUMNS:
            errs.append(f'{where}: 列が違う'); continue
        if r['leaf_id'] not in ok_leaf:
            errs.append(f'{where}: leaf_id {r["leaf_id"]!r} は assign=llm の葉ではない')
        if not QID.match(r['qid']):
            errs.append(f'{where}: qid {r["qid"]!r} の形式')
        if r['rel'] not in ('P31', 'P106'):
            errs.append(f'{where}: rel は P31 か P106')
        if not PROP.match(r['country_prop']):
            errs.append(f'{where}: country_prop {r["country_prop"]!r} の形式')
        if r['subclass'] not in ('0', '1'):
            errs.append(f'{where}: subclass は 0 か 1')
        if r['rel'] == 'P106' and r['subclass'] == '1':
            errs.append(f'{where}: P106 に subclass は使わない')
        if not r['max_rows'].isdigit() or not 1 <= int(r['max_rows']) <= 5000:
            errs.append(f'{where}: max_rows は 1〜5000')
        k = (r['leaf_id'], r['qid'], r['rel'])
        if k in seen:
            errs.append(f'{where}: 重複 {k}')
        seen.add(k)
    return errs


# ------------------------------------------------------------------ クエリ
def build_query(row, limit, offset):
    """日本に関係する(国/原産国/国籍が Japan Q17)項目で、公式サイト P856 を持ち、対応表のクラスに属するもの。"""
    cls = f'wdt:P31/wdt:P279*' if row['subclass'] == '1' and row['rel'] == 'P31' else f'wdt:{row["rel"]}'
    country = '|'.join(f'wdt:{p}' for p in row['country_prop'].split('|'))
    # ラベルは台帳に使わないので取らない(取ると ja ラベル複数の項目で行が増え、LIMIT/OFFSET の境目で抜け・重複が出る)。
    # P856 が複数ある項目も 1 行 1 URL にし、(?item ?url) で全順序にする。
    return (f'SELECT DISTINCT ?item ?url WHERE {{ ?item {cls} wd:{row["qid"]} ; ({country}) wd:Q17 ; wdt:P856 ?url . }} '
            f'ORDER BY ?item ?url LIMIT {int(limit)} OFFSET {int(offset)}')


def parse_results(data):
    """SPARQL JSON -> [(qid, url, label_ja)]。形が違う行は捨てる。"""
    out = []
    for b in (data.get('results') or {}).get('bindings') or []:
        item = (b.get('item') or {}).get('value', '')
        url = (b.get('url') or {}).get('value', '')
        if not item or not url:
            continue
        out.append((item.rsplit('/', 1)[-1], url, (b.get('label') or {}).get('value')))
    return out


def normalize_url(url):
    """公式サイトの URL -> (キー, ホスト, トップ URL)。http(s) 以外・IP・不正ホストは None。台帳の単位はホスト(パスは捨てる)。"""
    try:
        p = urllib.parse.urlsplit((url or '').strip())
        host = normalize_host(p.hostname)
    except ValueError:
        return None
    if p.scheme not in ('http', 'https') or not host or '.' not in host:
        return None
    return host_key(host), host, f'{p.scheme}://{host}/'


# ------------------------------------------------------------------ WDQS 呼び出し(キャッシュ・間隔・中断)
class Client:
    def __init__(self, cache_dir=CACHE_DIR, sleep=time.sleep, http_get=None, interval=WDQS_INTERVAL, clock=time.monotonic):
        self.cache_dir, self.sleep, self.interval, self.clock = cache_dir, sleep, interval, clock
        self.http_get = http_get or self._urllib_get
        self.last = None
        self.network_calls = 0

    @staticmethod
    def _urllib_get(url):
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/sparql-results+json'})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()[:2000]

    def _cache_path(self, query):
        return self.cache_dir / (hashlib.sha256(query.encode()).hexdigest()[:32] + '.json')

    def purge_old(self):
        """保持期間(CACHE_TTL_DAYS)の 2 倍より古いキャッシュを消す。"""
        if not self.cache_dir.exists():
            return 0
        limit, n = time.time() - 2 * CACHE_TTL_DAYS * 86400, 0
        for p in self.cache_dir.glob('*.json*'):
            try:
                if p.stat().st_mtime < limit:
                    p.unlink(); n += 1
            except OSError:
                pass
        return n

    def query(self, query):
        """結果 JSON を返す。キャッシュがあればネットワークに出ない。429/503 は WdqsAbort、その他の失敗は WdqsSkip。"""
        path = self._cache_path(query)
        if path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL_DAYS * 86400:
            return json.loads(path.read_text(encoding='utf-8'))
        if self.last is not None:
            wait = self.interval - (self.clock() - self.last)
            if wait > 0:
                self.sleep(wait)
        url = ENDPOINT + '?' + urllib.parse.urlencode({'query': query, 'format': 'json'})
        self.network_calls += 1
        try:
            status, headers, body = self.http_get(url)
        except (OSError, TimeoutError):   # urllib の URLError・タイムアウトも OSError。ネットワーク断は 1 行を諦める
            self.last = self.clock()
            raise WdqsSkip('network')
        self.last = self.clock()
        if status in (429, 503):
            ra = {k.lower(): v for k, v in headers.items()}.get('retry-after')
            raise WdqsAbort(status, parse_retry_after(ra))
        if status != 200:
            raise WdqsSkip(f'http_{status}')
        try:
            data = json.loads(body)
        except ValueError:
            raise WdqsSkip('bad_json')
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.part')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)
        return data


# ------------------------------------------------------------------ 発見
def listed_keys(root=ROOT):
    """既掲載のホストキー: 既存の掲載(data/collection-*.json)と、本番に出荷済み(var/nightly/shipped.txt)。"""
    keys = set()
    for p in sorted((root / 'data').glob('collection-*.json')):
        for r in json.loads(p.read_text(encoding='utf-8')):
            n = normalize_url(r.get('url'))
            if n:
                keys.add(n[0])
    shipped = root / 'var' / 'nightly' / 'shipped.txt'
    if shipped.exists():
        keys |= {l.strip() for l in shipped.read_text(encoding='utf-8').splitlines() if l.strip()}
    return keys


def cursor_name(row):
    return f'{row["leaf_id"]}:{row["rel"]}:{row["qid"]}'


def select_rows(rows, leaves=None, limit_leaves=None):
    """対応表の行を、指定された葉の順(leaves)で並べる。leaves が無ければ表の順。limit_leaves は葉の数の上限。"""
    order = list(leaves) if leaves else list(dict.fromkeys(r['leaf_id'] for r in rows))
    if limit_leaves is not None:
        order = order[:limit_leaves]
    return [r for leaf in order for r in rows if r['leaf_id'] == leaf]


def discover_wikidata(db, rows, client, max_rows=1000, page_size=PAGE_SIZE, listed=None, ignore_cursor=False, categories_path=CATEGORIES_PATH):
    """対応表の行ごとに、続きの OFFSET から max_rows(この 1 回の実行の合計)まで取り、台帳へ入れる。返り値は行ごとの統計と中断理由。"""
    errs = validate_map(rows, categories_path)
    if errs:
        raise ValueError('対応表が不正: ' + '; '.join(errs))
    client.purge_old()
    listed = listed if listed is not None else set()
    stats, remaining, aborted = [], max_rows, None
    for row in rows:
        if remaining <= 0:
            break
        cur = cursor_name(row)
        state = frontier.get_cursor(db, SOURCE, cur) or '0'
        if state == 'done' and not ignore_cursor:
            continue
        offset = 0 if state == 'done' else int(state)
        st = {'leaf_id': row['leaf_id'], 'qid': row['qid'], 'label_en': row['label_en'], 'fetched': 0, 'added': 0, 'dup_listed': 0, 'dup_ledger': 0, 'invalid': 0, 'status': 'ok'}
        stats.append(st)
        budget = min(int(row['max_rows']), remaining)
        done_row = False
        while st['fetched'] < budget:
            limit = min(page_size, budget - st['fetched'])
            try:
                data = client.query(build_query(row, limit, offset))
            except WdqsAbort as e:
                aborted = str(e); st['status'] = 'abort'; break
            except WdqsSkip as e:
                st['status'] = f'skip:{e}'; break
            got = parse_results(data)
            seen_items = set()
            for qid, url, label in got:
                n = normalize_url(url)
                if not n:
                    st['invalid'] += 1; continue
                key, host, top = n
                if key in listed:
                    st['dup_listed'] += 1; continue
                if frontier.add(db, key, host, top, SOURCE, f'wikidata:{qid} {row["qid"]}', 0, priority=priority(host) + 2, hint_leaf=row['leaf_id']):
                    st['added'] += 1
                else:
                    st['dup_ledger'] += 1
            st['fetched'] += len(got)
            offset += limit
            if len(got) < limit:      # 最後のページ(SPARQL の行数 < LIMIT)
                done_row = True; break
        remaining -= st['fetched']
        if st['status'] == 'ok' or st['fetched']:   # 失敗(abort/skip)で 1 件も取れなかった行は、カーソルを進めない
            frontier.set_cursor(db, SOURCE, cur, 'done' if done_row else offset)
        db.commit()
        if aborted:
            break
    frontier.set_cursor(db, SOURCE, 'last_run', f'rows={len(stats)} added={sum(s["added"] for s in stats)} aborted={aborted}')
    db.commit()
    return {'rows': stats, 'added': sum(s['added'] for s in stats), 'fetched': sum(s['fetched'] for s in stats), 'aborted': aborted, 'network_calls': client.network_calls}
