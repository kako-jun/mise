"""使用量ガード（#157）。GraphQL 応答は全てモック。ネットワーク・Cloudflare には出ない。"""
import datetime
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import usage_guard as ug

TOKEN, ACCT = 'tok_SECRET_VALUE_123', 'acct_SECRET_456'
TODAY = datetime.date(2026, 11, 19)


def day(n):
    return (TODAY - datetime.timedelta(days=n)).isoformat()


def api_body(workers=None, d1=None, other_workers=None, other_d1=None, pages=None, with_pages=True):
    """GraphQL 応答を作る。workers={date:n} は mise の Worker、other_workers={date:n} は同じアカウントの別の Worker。
    d1={date:(read, written)} は mise の D1、other_d1 は別のデータベース。pages={date:n} は Pages Functions のリクエスト。日別の別名（w<YYYYMMDD>）は 10〜11 月の全日分を作る。"""
    acc = {}
    for off in range(0, 70):
        d = datetime.date(2026, 10, 1) + datetime.timedelta(days=off)
        rows = []
        for name, src in (('mise', workers), ('other-worker', other_workers)):
            if src and d.isoformat() in src:
                rows.append({'sum': {'requests': src[d.isoformat()]}, 'dimensions': {'scriptName': name}})
        acc['w' + d.strftime('%Y%m%d')] = rows
        if with_pages:
            acc['p' + d.strftime('%Y%m%d')] = [{'sum': {'requests': (pages or {})[d.isoformat()]}}] if d.isoformat() in (pages or {}) else []
    rows = []
    for dbid, src in ((ug.D1_DATABASE_ID, d1), ('other-db-id', other_d1)):
        for d, (r, w) in (src or {}).items():
            rows.append({'sum': {'rowsRead': r, 'rowsWritten': w}, 'dimensions': {'date': d, 'databaseId': dbid}})
    acc['d1'] = rows
    return json.dumps({'data': {'viewer': {'accounts': [acc]}}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name)
        self.env = self.t / 'cf.env'
        self.env.write_text(f'# comment\nCLOUDFLARE_ANALYTICS_TOKEN={TOKEN}\nCLOUDFLARE_ACCOUNT_ID="{ACCT}"\n')
        self.out = io.StringIO()

    def go(self, post, **kw):
        with redirect_stdout(self.out):
            r = ug.run('2026-11-19', env_file=str(self.env), var_dir=str(self.t / 'var'), post=post, sleep=lambda s: None, out=lambda *a: print(*a), **kw)
        return r

    def assertNoSecret(self, r):
        blob = self.out.getvalue() + json.dumps(r, ensure_ascii=False, default=str)
        for f in (self.t / 'var').rglob('*.json'):
            blob += f.read_text()
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(ACCT, blob)


class Thresholds(Base):
    def test_judges_on_account_total_and_shows_mise_as_part(self):
        # アカウント全体の枠。mise 単独では 1% でも、他のアプリを含む合計で判定する
        r = self.go(lambda *a: (200, api_body({'2026-11-02': 100_000}, {'2026-11-02': (1_000_000, 10)},
                                              other_workers={'2026-11-02': 5_400_000}, other_d1={'2026-11-02': (22_000_000_000, 40_000_000)})))
        w, rd, wr = r['result']['workers_requests'], r['result']['d1_rows_read'], r['result']['d1_rows_written']
        self.assertEqual(w['month_total'], 5_500_000)
        self.assertEqual(w['level'], 'warn')
        self.assertEqual(w['mise_month_total'], 100_000)
        self.assertEqual(rd['month_total'], 22_001_000_000)
        self.assertEqual(rd['mise_month_total'], 1_000_000)
        self.assertEqual(wr['level'], 'alert')   # 40,000,010 / 50,000,000 = 80%
        self.assertTrue(any('アカウント全体' in g and 'Workers' in g for g in r['guards']))
        text = self.out.getvalue()
        self.assertIn('アカウント全体', text)
        self.assertIn('うち mise', text)
        self.assertEqual(text.count('うち mise'), 3)
        doc = json.loads((self.t / 'var' / 'usage' / '2026-11-19.json').read_text())
        self.assertIn('account-total', doc['scope'])
        self.assertEqual(doc['metrics']['workers_requests']['mise_month_total'], 100_000)

    def test_spike_uses_account_total(self):
        w = {day(i): 100_000 for i in range(2, 9)}
        o = {day(1): 800_000}     # 別アプリの急増でも検知する(アカウント合計)
        r = self.go(lambda *a: (200, api_body(w, {}, other_workers=o)))
        self.assertTrue(r['result']['workers_requests']['spike'])

    def test_ok_when_far_below_limits(self):
        r = self.go(lambda *a: (200, api_body({day(1): 1000}, {day(1): (10_000, 10)})))
        self.assertTrue(r['ok'] and r['configured'])
        self.assertEqual(r['guards'], [])
        self.assertEqual(r['result']['workers_requests']['level'], 'ok')
        self.assertTrue((self.t / 'var' / 'usage' / '2026-11-19.json').exists())
        self.assertNoSecret(r)

    def test_50_and_80_percent(self):
        # 当月累計: Workers 5,500,000 (55%)、D1 read 21 億... ではなく 80% 超 = 20,500,000,000
        r = self.go(lambda *a: (200, api_body({'2026-11-02': 5_500_000}, {'2026-11-02': (20_500_000_000, 100)})))
        self.assertEqual(r['result']['workers_requests']['level'], 'warn')
        self.assertEqual(r['result']['d1_rows_read']['level'], 'alert')
        self.assertTrue(any('Workers リクエスト' in g and '50%' in g for g in r['guards']))
        self.assertTrue(any('D1 読み取り行' in g and '80%' in g for g in r['guards']))

    def test_previous_month_not_counted(self):
        r = self.go(lambda *a: (200, api_body({'2026-10-30': 9_000_000, '2026-11-03': 10}, {})))
        self.assertEqual(r['result']['workers_requests']['month_total'], 10)

    def test_month_end_projection(self):
        # 完了した 18 日で 1,000,000 → 30 日換算 1,666,666 (17%)。D1 書き込みは 1 日 170 万 → 月末 5,100 万 (102%)
        daily_w = {f'2026-11-{i:02d}': 1_000_000 // 18 for i in range(1, 19)}
        daily_d = {f'2026-11-{i:02d}': (1, 1_700_000) for i in range(1, 19)}
        r = self.go(lambda *a: (200, api_body(daily_w, daily_d)))
        p = r['result']
        self.assertAlmostEqual(p['workers_requests']['projected_pct'], 16.7, delta=0.5)
        self.assertGreaterEqual(p['d1_rows_written']['projected_pct'], 99)
        self.assertTrue(any('月末予測' in g and 'D1 書き込み行' in g for g in r['guards']))
        self.assertFalse(any('月末予測' in g and 'Workers' in g for g in r['guards']))

    def test_today_partial_not_in_projection_but_in_total(self):
        r = self.go(lambda *a: (200, api_body({day(0): 700, day(1): 300}, {})))
        w = r['result']['workers_requests']
        self.assertEqual(w['month_total'], 1000)
        self.assertEqual(w['projected_month'], round(300 / 18 * 30))

    def test_first_of_month_has_no_projection(self):
        with redirect_stdout(self.out):
            r = ug.run('2026-12-01', env_file=str(self.env), var_dir=str(self.t / 'var'), post=lambda *a: (200, api_body({'2026-12-01': 5}, {})), sleep=lambda s: None)
        self.assertIsNone(r['result']['workers_requests']['projected_pct'])


class Spike(Base):
    def test_spike_vs_7_day_average(self):
        w = {day(i): 100_000 for i in range(2, 9)}
        w[day(1)] = 500_000    # 5 倍
        r = self.go(lambda *a: (200, api_body(w, {})))
        self.assertTrue(r['result']['workers_requests']['spike'])
        self.assertAlmostEqual(r['result']['workers_requests']['spike_ratio'], 5.0, places=1)
        self.assertTrue(any('急増' in g and 'Workers' in g for g in r['guards']))

    def test_no_spike_below_floor_or_ratio(self):
        small = {day(i): 10 for i in range(2, 9)}
        small[day(1)] = 1000          # 100 倍だが絶対量が小さい
        steady = {day(i): 100_000 for i in range(2, 9)}
        steady[day(1)] = 250_000      # 2.5 倍
        r = self.go(lambda *a: (200, api_body(small, {})))
        self.assertFalse(r['result']['workers_requests']['spike'])
        self.assertEqual(r['guards'], [])
        r = self.go(lambda *a: (200, api_body(steady, {})))
        self.assertFalse(r['result']['workers_requests']['spike'])

    def test_spike_from_zero_baseline_when_large(self):
        r = self.go(lambda *a: (200, api_body({day(1): 100_000}, {})))
        self.assertTrue(r['result']['workers_requests']['spike'])


class Pages(Base):
    """Pages Functions のリクエストも Workers のリクエスト枠に数えられる(公式の Pages Functions の料金ページ)。別のクエリで取って足す。"""
    def test_pages_added_to_workers_total_and_changes_verdict(self):
        w = {'2026-11-02': 4_000_000}
        r0 = self.go(lambda *a: (200, api_body(w, {})))
        self.assertEqual(r0['result']['workers_requests']['level'], 'ok')
        r = self.go(lambda *a: (200, api_body(w, {}, pages={'2026-11-02': 1_500_000})))
        x = r['result']['workers_requests']
        self.assertEqual(x['month_total'], 5_500_000)
        self.assertEqual(x['level'], 'warn')
        self.assertEqual((x['workers_month_total'], x['pages_month_total']), (4_000_000, 1_500_000))
        text = self.out.getvalue()
        self.assertIn('内訳: Workers 4,000,000 / Pages Functions 1,500,000', text)
        self.assertNoSecret(r)

    def test_pages_spike_counts(self):
        w = {day(i): 100_000 for i in range(2, 9)}
        r = self.go(lambda *a: (200, api_body(w, {}, pages={day(1): 900_000})))
        self.assertTrue(r['result']['workers_requests']['spike'])

    def test_pages_query_is_separate_from_workers_query(self):
        qs = []
        def post(url, body, token):
            qs.append(body['query'])
            return 200, api_body({day(1): 5}, {})
        self.go(post)
        self.assertEqual(len(qs), 2)
        self.assertIn('workersInvocationsAdaptive', qs[0]); self.assertNotIn('pagesFunctions', qs[0])
        self.assertIn('pagesFunctionsInvocationsAdaptiveGroups', qs[1]); self.assertNotIn('workersInvocationsAdaptive', qs[1])

    def test_pages_failure_warns_and_judges_on_workers_only(self):
        def post(url, body, token):
            if 'pagesFunctions' in body['query']:
                return 200, json.dumps({'errors': [{'message': f'Cannot query field "pagesFunctionsInvocationsAdaptiveGroups" {TOKEN} {ACCT}'}]})
            return 200, api_body({'2026-11-02': 5_500_000}, {})
        r = self.go(post)
        self.assertTrue(r['ok'])
        self.assertEqual(r['result']['workers_requests']['level'], 'warn')
        self.assertIsNone(r['result']['workers_requests']['pages_month_total'])
        self.assertTrue(any('Pages Functions は取得できず合計に含まれていません' in g for g in r['guards']))
        self.assertIn('取得できず', self.out.getvalue())
        self.assertNoSecret(r)

    def test_pages_http_error_also_only_warns(self):
        def post(url, body, token):
            return (500, 'x') if 'pagesFunctions' in body['query'] else (200, api_body({day(1): 5}, {}))
        r = self.go(post, )
        self.assertTrue(r['ok'])
        self.assertTrue(any('Pages Functions は取得できず' in g for g in r['guards']))

    def test_pages_missing_alias_is_a_warning_not_a_crash(self):
        r = self.go(lambda *a: (200, api_body({day(1): 5}, {}, with_pages=False)))
        self.assertTrue(r['ok'])
        self.assertTrue(any('Pages Functions は取得できず' in g for g in r['guards']))


class Edge(Base):
    def test_missing_day_alias_in_workers_response_is_unexpected_shape(self):
        body = json.loads(api_body({day(1): 5}, {}))
        del body['data']['viewer']['accounts'][0]['w' + day(1).replace('-', '')]
        r = self.go(lambda *a: (200, json.dumps(body)))
        self.assertFalse(r['ok'])
        self.assertTrue(any('想定外' in g for g in r['guards']))
        self.assertNoSecret(r)

    def test_mise_zero_days_show_zero_and_range_uses_next_day_lt(self):
        q = ug.build_query([TODAY])
        self.assertIn('datetime_lt: "2026-11-20T00:00:00Z"', q)
        self.assertNotIn('23:59:59', q)
        self.assertIn('datetime_lt: "2026-11-20T00:00:00Z"', ug.build_pages_query([TODAY]))
        r = self.go(lambda *a: (200, api_body({}, {}, other_workers={day(1): 1000})))
        x = r['result']['workers_requests']
        self.assertEqual((x['month_total'], x['mise_month_total'], x['mise_yesterday']), (1000, 0, 0))
        self.assertIn('うち mise', self.out.getvalue())


class Unconfigured(Base):
    def test_no_env_file_warns_and_returns_ok(self):
        with redirect_stdout(self.out):
            r = ug.run('2026-11-19', env_file=str(self.t / 'missing.env'), var_dir=str(self.t / 'var'), post=lambda *a: self.fail('API を呼んではいけない'))
        self.assertTrue(r['ok'])
        self.assertFalse(r['configured'])
        self.assertIn('未設定: 使用量ガードは動いていません。手順は docs/operations/usage-guard.md', r['guards'])
        self.assertFalse((self.t / 'var' / 'usage').exists())

    def test_missing_account_id_is_unconfigured(self):
        self.env.write_text(f'CLOUDFLARE_ANALYTICS_TOKEN={TOKEN}\n')
        r = self.go(lambda *a: self.fail('API を呼んではいけない'))
        self.assertFalse(r['configured'])
        self.assertNoSecret(r)

    def test_cli_exit_code_is_zero_without_token(self):
        with redirect_stdout(self.out):
            self.assertEqual(ug.main(['--env-file', str(self.t / 'missing.env'), '--var-dir', str(self.t / 'var'), '--date', '2026-11-19']), 0)
        self.assertIn('未設定', self.out.getvalue())


class ApiFailures(Base):
    def check_fails_softly(self, post, expect):
        r = self.go(post)
        self.assertFalse(r['ok'])
        self.assertTrue(r['configured'])
        self.assertTrue(any(expect in g for g in r['guards']), r['guards'])
        self.assertNoSecret(r)

    def test_429_retries_once_then_warns(self):
        n = []
        def post(*a):
            n.append(1)
            return 429, 'slow down'
        self.check_fails_softly(post, '429')
        self.assertEqual(len(n), 2)

    def test_5xx_retry_then_success(self):
        seq = [(503, 'x'), (200, api_body({day(1): 5}, {}))]
        r = self.go(lambda *a: seq.pop(0) if seq else (200, api_body({day(1): 5}, {})))
        self.assertTrue(r['ok'])

    def test_5xx_persistent(self):
        self.check_fails_softly(lambda *a: (502, 'bad gateway'), '502')

    def test_invalid_json(self):
        self.check_fails_softly(lambda *a: (200, '<html>not json'), 'JSON')

    def test_unexpected_shape(self):
        self.check_fails_softly(lambda *a: (200, json.dumps({'data': {'viewer': {'accounts': []}}})), '想定外')
        self.check_fails_softly(lambda *a: (200, json.dumps([1, 2])), '想定外')

    def test_graphql_errors_are_redacted(self):
        body = json.dumps({'errors': [{'message': f'bad account {ACCT} with token {TOKEN}'}]})
        self.check_fails_softly(lambda *a: (200, body), 'GraphQL')

    def test_auth_failure_hint(self):
        self.check_fails_softly(lambda *a: (403, 'forbidden'), 'Account Analytics')

    def test_network_error(self):
        def post(*a):
            raise urllib.error.URLError(f'cannot reach with {TOKEN}')
        self.check_fails_softly(post, '通信')

    def test_unexpected_exception_does_not_propagate(self):
        def post(*a):
            raise ValueError(TOKEN)
        # fetch_daily が捕まえない例外でも run は投げない
        self.check_fails_softly(post, '想定外のエラー')


class Constants(unittest.TestCase):
    def test_limits_match_official_pricing(self):
        # 出典: developers.cloudflare.com/d1/platform/pricing/ と /workers/platform/pricing/（2026-11-19）
        self.assertEqual(ug.METRICS['workers_requests']['limit'], 10_000_000)
        self.assertEqual(ug.METRICS['d1_rows_read']['limit'], 25_000_000_000)
        self.assertEqual(ug.METRICS['d1_rows_written']['limit'], 50_000_000)
        self.assertEqual((ug.WARN_PCT, ug.ALERT_PCT), (50.0, 80.0))

    def test_query_has_no_token_and_no_account_filter_by_script(self):
        q = ug.build_query([TODAY])
        self.assertNotIn('Bearer', q)
        self.assertNotIn('scriptName:', q.split('dimensions')[0].replace('workersInvocationsAdaptive', ''))   # scriptName は次元(内訳)にだけ使い、フィルタでは絞らない
        self.assertNotIn('databaseId:', q.split('filter:')[-1].split('}')[0])
        self.assertIn('dimensions { scriptName }', q)
        self.assertIn('$acct: string!', q)
        self.assertNotIn('dimensions { date }', q.split('d1:')[0])   # Workers に date 次元は使わない
        self.assertNotIn('pagesFunctions', q)


if __name__ == '__main__':
    unittest.main()
