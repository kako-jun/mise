#!/usr/bin/env python3
"""URL 発見ランナー: 見つけた URL を本番 D1 の url_progress(state='discovered')へ直接入れる(#235 / #39 / #128)。

ローカルに状態を持たない(SQLite の frontier・CSV・JSONL・キャッシュ・チェックポイントを作らない)。
続きの位置とリースは D1 の discovery_cursors(migration 0037)に置く。標準出力のレポートだけが出力。

  uv run python scripts/discover_online.py --source wikidata|wikidata-classes|official|links|cc|all [--limit N] [--dry-run | --apply]

既定は --dry-run(D1 を読み、供給元を取得し、入れるはずの件数をカテゴリ別に出す。何も書かない)。
Cloudflare REST は scripts/media_online.py の Cloudflare クライアント(CLOUDFLARE_API_TOKEN 必須)。
終了コード: 0 正常 / 1 検証不一致 / 2 リースが取れない・引数不正 / 3 Wikidata が 429/503 で中断 / 4 実行中にリースを失った。
"""
import argparse
import collections
import hashlib
import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / 'pipeline'))
from progress_identity import progress_identity_key
import discover_wikidata as dw
import fetcher
from hostnames import normalize_host, platform_page, registrable

MAX_PARAMS = 90               # D1 は 1 文 100 個まで。余裕を持たせる
INSERT_ROWS = 30              # 1 INSERT の行数(2 パラメータ/行)
BATCH_STATEMENTS = 5          # client.batch 1 回の文の数
LEASE_TTL = 1800              # 秒
REQUEST_INTERVAL = 0.3        # D1 へのリクエスト間隔(秒)
DIRECTORY_WAIT = 2.0          # 公式ディレクトリのページ取得間隔(秒)
WDQS_ABORT_EXIT = 3
LEASE_LOST_EXIT = 4
SOURCES = ('wikidata', 'wikidata-classes', 'official', 'links', 'cc')

INTERNAL_SUFFIXES = ('.local', '.localhost', '.internal', '.intranet', '.lan', '.home', '.corp', '.test', '.example', '.invalid', '.onion')
# 会員一覧から出る外部リンクのうち、会員の公式サイトではないもの(SNS・検索・動画・共有)
SKIP_HOSTS = ('facebook.com', 'twitter.com', 'x.com', 'instagram.com', 'youtube.com', 'youtu.be', 'line.me', 'tiktok.com', 'linkedin.com',
              'pinterest.com', 'note.com', 'google.com', 'google.co.jp', 'bing.com', 'yahoo.co.jp', 'yahoo.com', 'duckduckgo.com',
              'readspeaker.com', 'goo.gl', 'bit.ly', 'amazon.co.jp', 'rakuten.co.jp', 'apple.com', 'wikipedia.org')
SKIP_EXT = re.compile(r'\.(pdf|docx?|xlsx?|pptx?|zip|csv|jpe?g|png|gif|svg|mp4)$', re.I)

# 公式ディレクトリの表。URL はすべて各団体の公式ページ(取得して確認済みのものだけ)。
# kind は表示用のカテゴリ名。region=main は <main> / id=main|contents の範囲のリンクだけを使う。
OFFICIAL = [
    {'name': 'aja', 'label': '日本動画協会 会員社', 'category': 'anime', 'urls': ['https://aja.gr.jp/kaiin', 'https://aja.gr.jp/kaiin/associate']},
    {'name': 'zenginkyo', 'label': '全国銀行協会 会員一覧', 'category': 'banking', 'urls': ['https://www.zenginkyo.or.jp/abstract/outline/organization/member-01/']},
    {'name': 'jema', 'label': '日本電機工業会 会員一覧', 'category': 'electric-machinery', 'urls': ['https://www.jema-net.or.jp/about/members/index.html']},
    {'name': 'nikkenren', 'label': '日本建設業連合会 会員', 'category': 'construction', 'urls': ['https://www.nikkenren.com/about/kaiin.html']},
]
# 取得して調べたが、使える公開の会員リンク一覧が見つからなかったもの(推測で URL を作らない。理由つきで毎回レポートする)
UNVERIFIED = {
    'depart (日本百貨店協会)': '公開の会員一覧ページが見つからない',
    'jama (日本自動車工業会)': '会員リンク集に会員企業の公式サイトへのリンクが無い',
    'mintetsu (日本民営鉄道協会)': '会員一覧は会員ポータルのみ。リンク集は会員以外(自治体・団体)が混在',
    'jpma (日本製薬工業協会)': '会員会社ページに外部リンクが無い',
    'jsda (日本証券業協会)': '取得時に 429(rate limit)。再試行は人が間隔を空けて行う',
    'sonpo (日本損害保険協会)': '会員会社等一覧ページに会員企業へのリンクが無い',
    'j-hotel (日本ホテル協会)': '会員ホテル情報は外部検索へ委譲され、会員サイトへの直リンクが無い',
    'e-Gov 行政サービス一覧': '未調査(対象ページ未確定)',
}


# ------------------------------------------------------------------ URL 正規化
def host_ok(host):
    """normalize_host(IP・不正ラベルは None) + 内部名・単一ラベルの除外。"""
    host = normalize_host(host)
    if not host or '.' not in host or host.endswith(INTERNAL_SUFFIXES) or host.startswith('localhost'):
        return None
    return host


def candidate_from_url(url, keep_path=False):
    """URL -> (identity_key, url) | None。台帳の単位はホスト(既定はトップ URL。パスは捨てる)。"""
    try:
        p = urllib.parse.urlsplit((url or '').strip())
        host = host_ok(p.hostname)
        port = p.port
    except ValueError:
        return None
    if p.scheme not in ('http', 'https') or not host or port not in (None, 80, 443):
        return None
    if platform_page(url):   # ポータル/SNS 内の店舗・ユーザーページ
        return None
    out = f'{p.scheme}://{host}/' if not keep_path else f'{p.scheme}://{host}{p.path or "/"}' + (f'?{p.query}' if p.query else '')
    try:
        return progress_identity_key(out), out
    except ValueError:
        return None


def chunks(items, n):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


# ------------------------------------------------------------------ D1
class LeaseLost(RuntimeError):
    pass


class Lease:
    """discovery_cursors の source='lease' で 2 台が同時に動かないようにする。値は '<owner>|<期限 epoch 秒>'。"""
    def __init__(self, client, owner=None, ttl=LEASE_TTL, clock=time.time, key='discover'):
        self.client, self.ttl, self.clock, self.key = client, ttl, clock, key
        self.owner = owner or f'{platform.node() or "host"}-{uuid.uuid4().hex[:8]}'.replace('|', '_')
        self.held = False

    def _take(self):
        now = int(self.clock())
        self.client.query(
            "INSERT INTO discovery_cursors(source,cursor_key,cursor_value) VALUES('lease',?,?) "
            "ON CONFLICT(source,cursor_key) DO UPDATE SET cursor_value=excluded.cursor_value, updated_at=datetime('now') "
            "WHERE cursor_value IS NULL OR cursor_value LIKE ? || '|%' OR CAST(substr(cursor_value, instr(cursor_value,'|')+1) AS INTEGER) < ?",
            (self.key, f'{self.owner}|{now + self.ttl}', self.owner, now))
        rows = self.client.query("SELECT cursor_value FROM discovery_cursors WHERE source='lease' AND cursor_key=?", (self.key,))
        return bool(rows) and str(rows[0].get('cursor_value', '')).split('|', 1)[0] == self.owner

    def acquire(self):
        self.held = self._take()
        return self.held

    def renew(self):
        if not self._take():
            self.held = False
            raise LeaseLost('lease lost: 別の実行が引き継いだ')

    def release(self):
        if self.held:
            self.client.query("DELETE FROM discovery_cursors WHERE source='lease' AND cursor_key=? AND cursor_value LIKE ? || '|%'", (self.key, self.owner))
            self.held = False


class Cursors:
    """Wikidata のページング位置。dry-run では書かない。表が無ければ(migration 未適用)空として扱う。"""
    def __init__(self, client, write):
        self.client, self.write = client, write
        self.mem = {}
        self.missing_table = False

    def get(self, source, key):
        if (source, key) in self.mem:
            return self.mem[(source, key)]
        try:
            rows = self.client.query('SELECT cursor_value FROM discovery_cursors WHERE source=? AND cursor_key=?', (source, key))
        except RuntimeError as e:
            if 'no such table' in str(e) and not self.write:
                self.missing_table = True
                return None
            raise
        return rows[0]['cursor_value'] if rows else None

    def set(self, source, key, value):
        self.mem[(source, key)] = str(value)
        if self.write:
            self.client.query("INSERT INTO discovery_cursors(source,cursor_key,cursor_value) VALUES(?,?,?) "
                              "ON CONFLICT(source,cursor_key) DO UPDATE SET cursor_value=excluded.cursor_value, updated_at=datetime('now')",
                              (source, key, str(value)))


class Stats:
    def __init__(self):
        self.rows = collections.defaultdict(lambda: collections.Counter())
        self.categories = collections.defaultdict(lambda: collections.Counter())   # (source, category) -> Counter(added/known)
        self.notes = []
        self.mismatch = 0


class Sink:
    """候補を貯め、flush で ①ラン内重複 ②既知(url_progress / sites.progress_key)除外 ③INSERT OR IGNORE ④読み戻し検証 を行う。"""
    def __init__(self, client, apply, stats, lease=None, limit=None):
        self.client, self.apply, self.stats, self.lease, self.limit = client, apply, stats, lease, limit
        self.seen = set()          # このラン内で見た identity_key(メモリのみ)
        self.pending = []          # (key, url, source, category)
        self.added = collections.Counter()   # source -> 追加(予定)数

    def room_left(self, source):
        """あと何件候補を貯めてよいか(上限なしは None)。pending は既知・無効を含む保守的な数え方。"""
        if self.limit is None:
            return None
        return self.limit - self.added[source] - sum(1 for p in self.pending if p[2] == source)

    def room(self, source):
        left = self.room_left(source)
        return left is None or left > 0

    def offer(self, source, category, url, keep_path=False):
        st = self.stats.rows[source]
        st['fetched'] += 1
        cand = candidate_from_url(url, keep_path)
        if not cand:
            st['invalid'] += 1
            return False
        key, out = cand
        if key in self.seen:
            st['duplicate_in_run'] += 1
            return False
        self.seen.add(key)
        self.pending.append((key, out, source, category))
        return True

    def _known(self, keys):
        known = set()
        stmts = []
        for c in chunks(keys, MAX_PARAMS):
            stmts.append((f'SELECT identity_key AS k FROM url_progress WHERE identity_key IN ({",".join("?" * len(c))})', c))
        for c in chunks(keys, MAX_PARAMS // 2):   # sites.progress_key は末尾 '/' なしの旧形もありうる
            vals = c + [k.rstrip('/') for k in c]
            stmts.append((f'SELECT progress_key AS k FROM sites WHERE progress_key IN ({",".join("?" * len(vals))})', vals))
        for group in chunks(stmts, BATCH_STATEMENTS):
            for rows in self.client.batch(group):
                known |= {r['k'] for r in rows}
        known |= {k + '/' for k in known if not k.endswith('/')}
        return known

    def flush(self):
        if not self.pending:
            return
        pending, self.pending = self.pending, []
        if self.lease and self.apply:
            self.lease.renew()
        known = self._known([p[0] for p in pending])
        fresh = []
        for key, url, source, category in pending:
            st = self.stats.rows[source]
            if key in known:
                st['already_known'] += 1
                self.stats.categories[(source, category)]['known'] += 1
                continue
            if self.limit is not None and self.added[source] >= self.limit:
                st['over_limit'] += 1
                continue
            fresh.append((key, url, source, category))
            self.added[source] += 1
            self.stats.categories[(source, category)]['added' if self.apply else 'would_add'] += 1
        if not self.apply:
            for _, _, source, _ in fresh:
                self.stats.rows[source]['would_add'] += 1
            return
        stmts = []
        for c in chunks(fresh, INSERT_ROWS):
            vals = ','.join("(?,?,'discovered','discovered',datetime('now'),datetime('now'))" for _ in c)
            stmts.append((f'INSERT OR IGNORE INTO url_progress(identity_key,url,source,state,discovered_at,updated_at) VALUES{vals}',
                          [x for k, u, _, _ in c for x in (k, u)]))
        for group in chunks(stmts, BATCH_STATEMENTS):
            self.client.batch(group)
        present = set()
        vstmts = [(f'SELECT identity_key AS k FROM url_progress WHERE identity_key IN ({",".join("?" * len(c))})', [k for k, *_ in c])
                  for c in chunks(fresh, MAX_PARAMS)]
        for group in chunks(vstmts, BATCH_STATEMENTS):
            for rows in self.client.batch(group):
                present |= {r['k'] for r in rows}
        for key, _, source, _ in fresh:
            if key in present:
                self.stats.rows[source]['added'] += 1
                self.stats.rows[source]['verified'] += 1
            else:
                self.stats.rows[source]['added'] += 1
                self.stats.mismatch += 1
                self.stats.notes.append(f'verify mismatch: {key}')


# ------------------------------------------------------------------ 供給元: Wikidata
class WdqsClient:
    """WDQS への問い合わせ。キャッシュは持たない(メモリも持たない)。間隔・429/503 中断は discover_wikidata と同じ。"""
    def __init__(self, sleep=time.sleep, http_get=None, interval=dw.WDQS_INTERVAL, clock=time.monotonic):
        self.sleep, self.interval, self.clock = sleep, interval, clock
        self.http_get = http_get or dw.Client._urllib_get
        self.last = None
        self.network_calls = 0

    def query(self, query):
        if self.last is not None:
            wait = self.interval - (self.clock() - self.last)
            if wait > 0:
                self.sleep(wait)
        url = dw.ENDPOINT + '?' + urllib.parse.urlencode({'query': query, 'format': 'json'})
        self.network_calls += 1
        try:
            status, headers, body = self.http_get(url)
        except (OSError, TimeoutError):
            self.last = self.clock()
            raise dw.WdqsSkip('network')
        self.last = self.clock()
        if status in (429, 503):
            raise dw.WdqsAbort(status, dw.parse_retry_after({k.lower(): v for k, v in headers.items()}.get('retry-after')))
        if status != 200:
            raise dw.WdqsSkip(f'http_{status}')
        try:
            return json.loads(body)
        except ValueError:
            raise dw.WdqsSkip('bad_json')


def run_wikidata(sink, cursors, wdqs, rows, max_rows, page_size=dw.PAGE_SIZE):
    """対応表の行ごとに続きの OFFSET から取り、sink へ渡す。戻り値は中断理由(なければ None)。"""
    errs = dw.validate_map(rows)
    if errs:
        raise ValueError('対応表が不正: ' + '; '.join(errs))
    remaining, aborted = max_rows, None
    for row in rows:
        if remaining <= 0 or not sink.room('wikidata'):
            break
        cur = dw.cursor_name(row)
        state = cursors.get('wikidata', cur) or '0'
        if state == 'done':
            continue
        offset = int(state)
        fetched, done_row, ok = 0, False, True
        budget = min(int(row['max_rows']), remaining)
        while fetched < budget and sink.room('wikidata'):
            limit = min(page_size, budget - fetched)
            left = sink.room_left('wikidata')
            if left is not None:
                limit = min(limit, left)   # 上限で打ち切るページを作らない(カーソルより先の URL を捨てない)
            try:
                data = wdqs.query(dw.build_query(row, limit, offset))
            except dw.WdqsAbort as e:
                aborted = str(e); ok = False; break
            except dw.WdqsSkip as e:
                sink.stats.notes.append(f'wikidata {cur}: skip {e}'); ok = False; break
            got = dw.parse_results(data)
            for qid, url, _label in got:
                sink.offer('wikidata', row['leaf_id'], url)
            fetched += len(got)
            offset += limit
            if len(got) < limit:
                done_row = True; break
        remaining -= fetched
        sink.flush()   # 入れて検証してからカーソルを進める
        if sink.stats.mismatch:
            break
        if ok or fetched:
            cursors.set('wikidata', cur, 'done' if done_row else offset)
        if aborted:
            break
    return aborted


# ------------------------------------------------------------------ 供給元: Wikidata のクラス表(wikidata-classes)
# 1 行 = 1 つの束ね(カテゴリ)。qids は根のクラスで、そのサブクラス(P279*)の P31 を持つ項目をすべて対象にする(複数は OR)。
# country は国の性質(P17=国 / P495=原産国)で Japan(Q17)に絞る。None なら世界全体。
# 知名度(言語版リンク数など)では絞らない。公式サイト P856 を持つ項目は個人・零細も含めて全部対象(mise は規模で差別しない)。
# 終了・統合済み(P576/P582)も含めるので、終了日でも絞らない。除くのは candidate_from_url の機械的な拒否だけ(SNS・ポータル・内部 IP・非 80/443 など)。
# 並びは項目 ID の文字列順のキーセット。カーソルは「最後に取った項目 ID」で、1 回の実行は --limit / --max-rows で区切って何回かに分けて回す。
# ここを足す・直すときは、必ず実 WDQS で件数と上位を確認する(docs/operations/nightly.md)。
WIKIDATA_CLASSES = [
    # --- 日本の企業・組織(P17=Japan)
    {'name': 'jp-game-company', 'category': 'game-company', 'qids': ['Q210167', 'Q1137109', 'Q43400508'], 'country': 'P17'},
    {'name': 'jp-company', 'category': 'company', 'qids': ['Q783794'], 'country': 'P17'},   # business(Q4830453) の P279* は WDQS の 60 秒を超える
    {'name': 'jp-finance', 'category': 'finance-insurance', 'qids': ['Q22687', 'Q43183', 'Q3591545'], 'country': 'P17'},
    {'name': 'jp-construction-realestate', 'category': 'construction-realestate', 'qids': ['Q811930', 'Q1660104'], 'country': 'P17'},
    {'name': 'jp-transport-energy', 'category': 'transport-energy', 'qids': ['Q249556', 'Q46970'], 'country': 'P17'},
    {'name': 'jp-medical-pharma', 'category': 'medical-pharma', 'qids': ['Q19644607', 'Q16917'], 'country': 'P17'},
    {'name': 'jp-broadcaster', 'category': 'tv-radio', 'qids': ['Q1616075', 'Q14350', 'Q15265344'], 'country': 'P17'},
    {'name': 'jp-newspaper-publisher', 'category': 'newspaper-publisher', 'qids': ['Q11032', 'Q2085381', 'Q1320047'], 'country': 'P17'},
    {'name': 'jp-anime-studio', 'category': 'anime-studio', 'qids': ['Q1107679'], 'country': 'P17'},
    {'name': 'jp-education', 'category': 'education', 'qids': ['Q3918'], 'country': 'P17'},
    {'name': 'jp-public-service', 'category': 'public-service', 'qids': ['Q327333', 'Q2659904'], 'country': 'P17'},
    # --- 海外の企業・Web サービス(世界全体)。規模・知名度では選ばない。根クラスを狭く 1 行 1 根にしているのは WDQS のクエリ費用(60 秒制限)で区切るためだけ
    {'name': 'global-public-company', 'category': 'global-company', 'qids': ['Q891723'], 'country': None},
    {'name': 'global-multinational', 'category': 'global-company', 'qids': ['Q161726'], 'country': None},
    {'name': 'global-conglomerate', 'category': 'global-company', 'qids': ['Q778575'], 'country': None},
    {'name': 'global-tech-company', 'category': 'global-company', 'qids': ['Q18388277'], 'country': None},
    {'name': 'global-startup', 'category': 'global-company', 'qids': ['Q129238'], 'country': None},
    {'name': 'global-software-company', 'category': 'web-service', 'qids': ['Q1058914'], 'country': None},
    {'name': 'global-online-shop', 'category': 'web-service', 'qids': ['Q4382945'], 'country': None},
    {'name': 'global-web-service', 'category': 'web-service', 'qids': ['Q193424'], 'country': None},
    {'name': 'global-sns', 'category': 'web-service', 'qids': ['Q3220391'], 'country': None},
    {'name': 'global-streaming', 'category': 'web-service', 'qids': ['Q63241860'], 'country': None},
    {'name': 'global-marketplace', 'category': 'web-service', 'qids': ['Q3390477'], 'country': None},
    {'name': 'global-mobile-app', 'category': 'web-service', 'qids': ['Q620615'], 'country': None},
    # --- 独自の公式サイトを持つタイトル・IP
    {'name': 'jp-video-game', 'category': 'game-title', 'qids': ['Q7889', 'Q7058673'], 'country': 'P495'},
    {'name': 'jp-film-anime', 'category': 'film-anime-title', 'qids': ['Q11424', 'Q63952888'], 'country': 'P495'},
    {'name': 'jp-franchise-manga', 'category': 'ip-franchise', 'qids': ['Q196600', 'Q21198342'], 'country': 'P495'},
    {'name': 'global-video-game', 'category': 'global-title-ip', 'qids': ['Q7889'], 'country': None},
    {'name': 'global-film', 'category': 'global-title-ip', 'qids': ['Q11424'], 'country': None},
    {'name': 'global-franchise', 'category': 'global-title-ip', 'qids': ['Q196600'], 'country': None},
]
CLASS_PAGE_SIZE = 200
_QID = re.compile(r'^Q[1-9]\d{0,9}$')
_PROP = re.compile(r'^P[1-9]\d{0,5}$')


def validate_classes(rows):
    errs, names = [], set()
    for i, r in enumerate(rows):
        w = f'行{i}'
        if not re.match(r'^[a-z][a-z0-9-]*$', str(r.get('name', ''))) or r['name'] in names:
            errs.append(f'{w}: name の形式・重複')
        names.add(r.get('name'))
        if not r.get('category') or not r.get('qids') or not all(_QID.match(q) for q in r['qids']):
            errs.append(f'{w}: category / qids')
        if r.get('country') is not None and not _PROP.match(str(r['country'])):
            errs.append(f'{w}: country は P 番号か None(キーが無ければ世界全体)')
        if set(r) - {'name', 'category', 'qids', 'country'}:
            errs.append(f'{w}: 未知のキー(知名度の足切りなどは持たない)')
    return errs


def class_cursor_name(row):
    """クラス・国が変わったら別カーソルにする(古い位置を新条件に流用しない)。"""
    sig = hashlib.sha1(f'{",".join(row["qids"])}|{row.get("country")}'.encode()).hexdigest()[:6]
    return f'{row["name"]}:{sig}'


ENTITY = 'http://www.wikidata.org/entity/'


def build_class_query(row, limit, after):
    """根クラスのサブクラスの P31 で、公式サイト P856 を持つ項目の ID を、IRI の文字列順で after(例 'Q123'。空は先頭)より後ろから limit 件。
    重い部分(クラスの辿り・並べ替え)だけをここで行い、URL は build_urls_query で別に引く(1 クエリを 60 秒の制限に収めるため)。"""
    country = f'?item wdt:{row["country"]} wd:Q17 . ' if row.get('country') else ''
    roots = ' '.join(f'wd:{q}' for q in row['qids'])
    return (f'SELECT DISTINCT ?item WHERE {{ VALUES ?root {{ {roots} }} ?cls wdt:P279* ?root . ?item wdt:P31 ?cls . {country}?item wdt:P856 [] . '
            f'FILTER(STR(?item) > "{ENTITY}{after}") }} ORDER BY STR(?item) LIMIT {int(limit)}')


def build_urls_query(qids):
    """項目 ID の一覧 -> 1 項目 1 URL(P856 が複数なら文字列順で最小の 1 つ。--limit で行が切れて URL を落とさないため)。"""
    return ('SELECT ?item (MIN(STR(?u)) AS ?url) WHERE { VALUES ?item { ' + ' '.join(f'wd:{q}' for q in qids) + ' } ?item wdt:P856 ?u } GROUP BY ?item ORDER BY STR(?item)')


def parse_item_ids(data):
    out = []
    for b in (data.get('results') or {}).get('bindings') or []:
        v = (b.get('item') or {}).get('value', '')
        if v.startswith(ENTITY) and _QID.match(v[len(ENTITY):]):
            out.append(v[len(ENTITY):])
    return out


def run_wikidata_classes(sink, cursors, wdqs, rows, max_rows, page_size=CLASS_PAGE_SIZE, source='wikidata-classes'):
    """クラスごとに、カーソル(最後に取った項目 ID)より後ろを取り、sink へ渡す。戻り値は中断理由(なければ None)。"""
    errs = validate_classes(rows)
    if errs:
        raise ValueError('クラス表が不正: ' + '; '.join(errs))
    remaining, aborted = max_rows, None
    for row in rows:
        if remaining <= 0 or not sink.room(source):
            break
        cur = class_cursor_name(row)
        state = cursors.get(source, cur) or ''
        if state != 'done' and state != '' and not _QID.match(state):
            sink.stats.notes.append(f'{source} {cur}: カーソル値が不正({state[:20]!r})。先頭から取り直す')
            state = ''
        if state == 'done':
            continue
        after, fetched, done_row, ok = state, 0, False, True
        while remaining - fetched > 0 and sink.room(source):
            limit = min(page_size, remaining - fetched)
            left = sink.room_left(source)
            if left is not None:
                limit = min(limit, left)   # 上限で打ち切るページを作らない(カーソルより先の URL を捨てない)
            try:
                ids = parse_item_ids(wdqs.query(build_class_query(row, limit, after)))
                got = dw.parse_results(wdqs.query(build_urls_query(ids))) if ids else []
            except dw.WdqsAbort as e:
                aborted = str(e); ok = False; break
            except dw.WdqsSkip as e:
                sink.stats.notes.append(f'{source} {cur}: skip {e}'); ok = False; break
            for _qid, url, _label in got:
                sink.offer(source, row['category'], url)
            fetched += len(ids)
            if ids:
                after = max(ids)   # 文字列順(WDQS の ORDER BY STR(?item) と同じ)
            if len(ids) < limit:
                done_row = True; break
        remaining -= fetched
        sink.flush()   # 入れて検証してからカーソルを進める
        if sink.stats.mismatch:
            break
        if ok or fetched:
            cursors.set(source, cur, 'done' if done_row else after)
        if aborted:
            break
    return aborted


# ------------------------------------------------------------------ 供給元: 公式ディレクトリ
def main_region(html):
    """<main> か id=main/contents の範囲(ヘッダー・フッターのナビを除く)。無ければ全体。"""
    m = re.search(r'<main\b', html, re.I)
    if m:
        e = re.search(r'</main>', html[m.end():], re.I)
        return html[m.start(): m.end() + e.start()] if e else html[m.start():]
    m = re.search(r'''id=["'](?:main|contents)["']''', html)
    if m:
        e = re.search(r'<footer\b', html[m.end():], re.I)
        return html[m.start(): m.end() + e.start()] if e else html[m.start():]
    return html


def extract_member_links(html, page_url):
    """ディレクトリのページから会員の外部サイトのリンクを取る。ディレクトリ自身の登録ドメイン・SNS・検索・PDF・mailto は除く。"""
    own = urllib.parse.urlsplit(page_url).hostname or ''
    own_root = registrable(own.lower())
    out = []
    for m in re.finditer(r'''<a\b[^>]*?\bhref=["']([^"']+)["']''', main_region(html), re.I):
        href = m.group(1).strip()
        if href.startswith(('mailto:', 'tel:', 'javascript:', '#')):
            continue
        u = urllib.parse.urlsplit(urllib.parse.urljoin(page_url, href))
        host = (u.hostname or '').lower()
        if u.scheme not in ('http', 'https') or not host or host == own_root or host.endswith('.' + own_root):
            continue
        if any(host == s or host.endswith('.' + s) for s in SKIP_HOSTS) or SKIP_EXT.search(u.path):
            continue
        out.append(u.geturl())
    return out


class PoliteFetcher:
    """robots.txt を尊重し、リクエスト間隔をあけてページ HTML を取る(fetcher.http_get / RobotsCache を再利用。SSRF 検査つき)。"""
    def __init__(self, sleep=time.sleep, wait=DIRECTORY_WAIT):
        self.sleep, self.wait = sleep, wait
        self.robots = fetcher.RobotsCache(lambda host: self.sleep(self.wait))
        self.requests = 0

    def get(self, url):
        """-> (html|None, reason|None)"""
        p = urllib.parse.urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        try:
            st, ok, delay = self.robots.check(origin, p.path or '/')
            if st == 'unavailable' or not ok:
                return None, 'robots_disallowed' if st != 'unavailable' else 'robots_unavailable'
            for _ in range(fetcher.MAX_REDIRECTS + 1):
                self.sleep(max(self.wait, delay))
                self.requests += 1
                status, headers, body = fetcher.http_get(url)
                if 300 <= status < 400 and headers.get('location'):
                    nxt = urllib.parse.urljoin(url, headers['location'])
                    if not fetcher.same_site(urllib.parse.urlsplit(nxt).hostname or '', p.hostname or ''):
                        return None, 'redirect_offsite'
                    url = nxt
                    continue
                if status != 200:
                    return None, f'http_{status}'
                return fetcher.decode_body(body, headers), None
            return None, 'too_many_redirects'
        except fetcher.FetchError as e:
            return None, e.code


def run_official(sink, pf, adapters=None):
    for ad in (OFFICIAL if adapters is None else adapters):
        if sink.stats.mismatch:
            sink.stats.notes.append('official: 照合不一致のため中断')
            break
        if not sink.room('official'):
            break
        got = 0
        for url in ad['urls']:
            html, reason = pf.get(url)
            if html is None:
                sink.stats.notes.append(f'official {ad["name"]}: {url} 取得できず ({reason})')
                continue
            links = extract_member_links(html, url)
            if not links:
                sink.stats.notes.append(f'official {ad["name"]}: {url} 会員リンクが 0 件(ページ構造が変わった可能性)')
            for link in links:
                sink.offer('official', ad['category'], link)
            got += len(links)
            sink.flush()
        sink.stats.notes.append(f'official {ad["name"]} ({ad["label"]}): リンク {got} 件')


# ------------------------------------------------------------------ 実行・レポート
def report(stats, apply, out=print):
    out(f'== mode: {"APPLY" if apply else "DRY-RUN (何も書いていない)"}')
    cols = ['fetched', 'invalid', 'duplicate_in_run', 'already_known', 'over_limit', 'would_add' if not apply else 'added', 'verified']
    out('source     ' + ' '.join(f'{c:>16}' for c in cols))
    for source in SOURCES:
        if source in stats.rows:
            out(f'{source:10} ' + ' '.join(f'{stats.rows[source][c]:>16}' for c in cols))
    out('== category (source / category: new / already known)')
    for (source, cat), c in sorted(stats.categories.items()):
        out(f'{source:9} {cat:22} new={c["added"] + c["would_add"]} known={c["known"]}')
    for n in stats.notes:
        out(f'note: {n}')
    out('== 未確認の公式ディレクトリ(推測で URL を作らない)')
    for k, v in UNVERIFIED.items():
        out(f'unverified: {k}: {v}')
    if stats.mismatch:
        out(f'!! verify mismatch: {stats.mismatch} 件')


def run(client, sources, apply, limit, max_rows=1000, wdqs=None, pf=None, rows=None, out=print, lease=None, extra=None, class_rows=None):
    stats = Stats()
    lease = lease or Lease(client)
    if apply and not lease.acquire():
        out('lease: 別の実行が動いている。何もせず終了')
        return 2, stats
    aborted, lost = None, False
    try:
        cursors = Cursors(client, apply)
        sink = Sink(client, apply, stats, lease if apply else None, limit)
        wdqs = wdqs or WdqsClient()   # wikidata と wikidata-classes で同じクライアント(5 秒間隔を共有)
        if 'wikidata' in sources:
            aborted = run_wikidata(sink, cursors, wdqs, rows if rows is not None else dw.load_map(), max_rows)
            if aborted:
                stats.notes.append(f'wikidata 中断: {aborted}')
        if 'wikidata-classes' in sources and not aborted:
            if stats.mismatch:
                stats.notes.append('wikidata-classes: 照合不一致のため書き込みを止めた')
            else:
                aborted = run_wikidata_classes(sink, cursors, wdqs, class_rows if class_rows is not None else WIKIDATA_CLASSES, max_rows)
                if aborted:
                    stats.notes.append(f'wikidata-classes 中断: {aborted}')
        if cursors.missing_table:
            stats.notes.append('discovery_cursors が未適用(migration 0037)。カーソルは先頭扱い')
        if 'official' in sources:
            if stats.mismatch:
                stats.notes.append('official: 照合不一致のため書き込みを止めた')
            else:
                run_official(sink, pf or PoliteFetcher())
        if 'links' in sources or 'cc' in sources:   # scripts/discover_links_cc.py(links: 公開済みサイトの外部リンク / cc: Common Crawl ホストグラフ)
            import discover_links_cc
            discover_links_cc.run_sources(sink, cursors, sources, client, pf or PoliteFetcher(), extra)
        sink.flush()
    except LeaseLost as e:
        lost = True
        stats.notes.append(str(e))
    finally:
        if apply:
            try:
                lease.release()
            except Exception as e:   # 解放の失敗で元のエラーを隠さない
                stats.notes.append(f'lease release failed: {e}')
    report(stats, apply, out)
    if lost:
        return LEASE_LOST_EXIT, stats
    if stats.mismatch:
        return 1, stats
    return (WDQS_ABORT_EXIT if aborted else 0), stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', choices=['wikidata', 'wikidata-classes', 'official', 'links', 'cc', 'all'], default='all')
    ap.add_argument('--limit', type=int, default=None, help='供給元ごとの追加(予定)件数の上限')
    ap.add_argument('--max-rows', type=int, default=1000, help='wikidata / wikidata-classes: 1 回に取る SPARQL 行数(項目数)の合計の上限')
    ap.add_argument('--max-sites', type=int, default=100, help='links: 1 回に処理する公開済みサイト数の上限(sites.id 昇順で続きから)')
    ap.add_argument('--cc-sample-every', type=int, default=1000, help='cc: ホスト名ハッシュで 1/N を決定論的に抽出')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='既定。何も書かない')
    mode.add_argument('--apply', action='store_true', help='url_progress へ書く')
    a = ap.parse_args(argv)
    if a.limit is not None and a.limit < 1:
        ap.error('--limit は 1 以上')
    if a.cc_sample_every < 1:
        ap.error('--cc-sample-every は 1 以上')
    import media_online
    client = media_online.Cloudflare(min_request_interval=REQUEST_INTERVAL)
    sources = SOURCES if a.source == 'all' else (a.source,)
    code, _ = run(client, sources, a.apply, a.limit, a.max_rows, extra={'max_sites': a.max_sites, 'cc_sample_every': a.cc_sample_every})
    return code


if __name__ == '__main__':
    sys.exit(main())
