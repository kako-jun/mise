import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { inlineScriptHashes, pageCsp } from '../src/security.js';
import { wantsNoindex, isFeedView } from '../src/seo.js';

// 縦フィードの試作ビュー(?view=feed。#52)。DOM を使う IIFE なので、通常表示を変えないこと・CSP と第三者アクセスの規約を静的に守らせる。
const read = f => readFileSync(new URL('../public/' + f, import.meta.url), 'utf8');
const html = read('index.html'), appJs = read('feed.js'), libJs = read('feed-lib.js'), css = read('feed.css');
const js = appJs + '\n' + libJs;
const lib = (() => { const ctx = { globalThis: null, URL, Date, Math, Number, Object, JSON, Array, String, Map }; ctx.globalThis = ctx; vm.runInNewContext(libJs, ctx); return ctx.MiseFeedLib; })();

test('index.html: 試作ビューの読み込みフックは ?view=feed のときだけ feed.css / feed.js を足す', () => {
  const hook = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).find(s => s.includes('feed.js'));
  assert.ok(hook, 'フックがある');
  assert.match(hook, /get\("view"\) === "feed"/);
  assert.match(hook, /feed-lib\.js[\s\S]*feed\.js/, 'lib を先に読む');
  assert.match(hook, /async = false/, '動的 script は順序保証のため async=false');
  assert.ok(!/<(link|script)[^>]+feed/.test(html), '通常表示は feed を静的に読み込まない');
  assert.ok(!/<(link|script)[^>]+feed\./.test(html), 'タグとして直接は読まない');
});

test('CSP: フックのインラインスクリプトはハッシュで許可され、feed.js は同一オリジン(script-src self)で読める', async () => {
  const hashes = await inlineScriptHashes(html);
  const csp = pageCsp(hashes);
  assert.match(csp, /script-src 'self' 'sha256-/);
  assert.ok(hashes.length >= 3);
  assert.match(csp, /style-src 'self'(;|$)/, 'インライン style は許さない');
});

test('feed.js: 第三者へアクセスしない(外部 URL・https サムネ・eval・インライン style 属性を持たない)', () => {
  const code = js.replace(/^\s*\/\/.*$/gm, '');
  const urls = [...code.matchAll(/https?:\/\/[^\s'"`)]+/g)].map(m => m[0]);
  assert.deepEqual(urls.filter(u => u !== 'http://www.w3.org/2000/svg'), [], 'SVG 名前空間以外の絶対 URL なし');
  assert.ok(!/\beval\(|new Function\(|setAttribute\('style'|\.cssText/.test(code));
  assert.ok(!/@import|url\(\s*['"]?https?:/.test(css), 'css も外部を読まない');
});

test('feed.js: 端末内保存は try/catch で包む(使えなくても動く)', () => {
  const direct = js.split('\n').filter(l => /\b(localStorage|sessionStorage|indexedDB)\s*[.[]/.test(l) && !/try\s*\{/.test(l) && !/^\s*\/\//.test(l));
  assert.deepEqual(direct, [], 'storage への直接アクセスは try の行だけ');
});

test('feed.js: public/ に画像を置かない規約(.webp は R2 経由)のため、モックのサムネ読み込みは持たない', () => {
  assert.ok(!/_mock-thumbs/.test(js));
});

test('thumbOf: 同一オリジンの絶対パスだけ(//host・/\\host・https・data は不可)', () => {
  const O = 'https://mise.example';
  for (const ok of ['/thumb/a.webp', '/x/y.png']) assert.equal(lib.thumbOf(ok, O), ok, ok);
  for (const ng of ['https://evil.example/a.png', '//evil.example/a.png', '/\\evil.example/a.png', '/\\\\evil.example/a.png', 'data:image/png;base64,AA', 'javascript:alert(1)', 'a.png', '', null, undefined, 5, '/a\n.png']) assert.equal(lib.thumbOf(ng, O), null, String(ng));
});

test('safeHttpUrl: http(s) だけ。javascript: / data: / vbscript: / 相対 / 不正は null。カードは安全な URL のときだけ開くリンクを作る', () => {
  assert.equal(lib.safeHttpUrl('https://example.jp/a'), 'https://example.jp/a');
  assert.equal(lib.safeHttpUrl('http://example.jp'), 'http://example.jp/');
  for (const bad of ['javascript:alert(1)', 'JavaScript:alert(1)', ' javascript:alert(1)', 'data:text/html,x', 'vbscript:msgbox(1)', '/relative/path', 'example.jp', '//example.jp', 'not a url', '', null, undefined]) assert.equal(lib.safeHttpUrl(bad), null, String(bad));
  assert.match(appJs, /const href = L\.safeHttpUrl\(s\.url\);\s*if \(href\) \{ const a = document\.createElement\('a'\); a\.className = 'f-open'; a\.href = href;/);
  assert.ok(!/\.href = s\.url/.test(appJs));
});

test('長文: 実データが無いカードに長文欄を出さない。ダミーは ?mock=1 のときだけ', () => {
  assert.match(appJs, /const useMock = params\.get\('mock'\) === '1';/);
  assert.match(appJs, /if \(!useMock\) return null;/);
  assert.match(appJs, /const lt = longText\(s\);\s*if \(lt\) \{/);
});

test('order: 同じ seed・now なら同じ並び。new は更新の新しい順。入力を壊さない', () => {
  const now = Date.parse('2026-11-18T00:00:00Z');
  const sites = Array.from({ length: 50 }, (_, i) => ({ id: 's' + String(i).padStart(2, '0'), source_updated_at: new Date(now - i * 86400000 * 5).toISOString() }));
  const o = (sort, seed) => Array.from(lib.order(sites, { sort, seed, now, boost: 1.5, halfLifeDays: 60 }), s => s.id);
  assert.deepEqual(o('mix', 'abc'), o('mix', 'abc'));
  assert.notDeepEqual(o('mix', 'abc'), o('mix', 'abd'));
  assert.deepEqual([...o('mix', 'abc')].sort(), sites.map(s => s.id).sort(), '全件を重複なく並べる');
  assert.deepEqual(o('new', 'x').slice(0, 3), ['s00', 's01', 's02']);
  assert.equal(sites[0].id, 's00');
  // 新しいサイトほど先頭に来やすい(多数の seed の平均順位)
  const avg = id => { let t = 0; for (let k = 0; k < 200; k++) t += o('mix', 'k' + k).indexOf(id); return t / 200; };
  assert.ok(avg('s00') < avg('s49'));
});

test('inScope / covers: 範囲の絞り込みと、取り直しが要るかの判定', () => {
  const tokyo = { country_code: 'JP', region_code: 'JP-13' }, us = { country_code: 'US', region_code: 'US-CA' };
  assert.ok(lib.inScope(tokyo, 'all') && lib.inScope(tokyo, 'jp') && lib.inScope(tokyo, 'jp-13'));
  assert.ok(!lib.inScope(us, 'jp') && !lib.inScope(tokyo, 'jp-14'));
  assert.ok(lib.covers('all', true, 'jp-13') && lib.covers('jp', true, 'jp-13') && lib.covers('jp-13', false, 'jp-13'));
  assert.ok(!lib.covers('all', false, 'jp'), '上限で切れた読み込みは範囲を含むとは言えない');
  assert.ok(!lib.covers('jp', true, 'all') && !lib.covers('jp', true, 'us') && !lib.covers('jp', true, 'jp2-1'));
  assert.ok(!lib.covers(null, false, 'jp'));
});

test('「見た」JSON の読み込み: 1MB 超・形違い・不正なキーは拒否/除外', () => {
  const ok = JSON.stringify({ v: 1, sites: { 'collected-a': '2026-11-01T00:00:00Z', 'dev-seed-00001': '2026-11-02T00:00:00Z' } });
  assert.deepEqual(Object.keys(lib.parseSeenImport(ok)), ['collected-a', 'dev-seed-00001']);
  const mixed = lib.parseSeenImport(JSON.stringify({ v: 1, sites: { 'ok-id': '2026-11-01', '<script>': '2026-11-01', 'a b': '2026-11-01', ['x'.repeat(129)]: '2026-11-01', 'bad-date': 'zzz', 'num': 5 } }));
  assert.deepEqual(Object.keys(mixed), ['ok-id']);
  assert.equal(lib.parseSeenImport('x'.repeat(lib.SEEN_MAX_BYTES + 1)), null);
  for (const bad of ['', 'not json', '[]', 'null', '{"v":2,"sites":{}}', '{"v":1,"sites":[]}', '{"v":1}', 5]) assert.equal(lib.parseSeenImport(bad), null, String(bad));
  assert.match(appJs, /f\.size > L\.SEEN_MAX_BYTES/);
});

test('dwell: 一覧を作り直すとき clearAll() で古いタイマーが発火しない(見ていないサイトを数えない)', () => {
  const timers = []; let id = 0;
  const fake = { setTimeout: (fn, ms) => { timers.push({ id: ++id, fn, ms, live: true }); return id; }, clearTimeout: t => { const x = timers.find(y => y.id === t); if (x) x.live = false; } };
  const seen = []; const d = lib.makeDwell(1500, s => seen.push(s), fake);
  d.start('el1', 'site-1'); d.start('el2', 'site-2'); assert.equal(d.size(), 2);
  d.clearAll(); assert.equal(d.size(), 0);
  for (const t of timers) if (t.live) t.fn();
  assert.deepEqual(seen, [], 'clearAll 後は誰も数えない');
  d.start('el3', 'site-3'); d.stop('el3'); for (const t of timers) if (t.live) t.fn();
  assert.deepEqual(seen, []);
  d.start('el4', 'site-4'); for (const t of timers) if (t.live) t.fn();
  assert.deepEqual(seen, ['site-4']);
  assert.match(appJs, /function resetList\(\) \{\s*observer && observer\.disconnect\(\); dwell\.clearAll\(\);/);
});

test('seo: ?view=feed は noindex、無ければ付かない(検索結果の noindex は従来どおり)', () => {
  const sp = q => new URL('https://x.example/jp/' + q).searchParams;
  assert.equal(isFeedView(sp('?view=feed')), true);
  assert.equal(wantsNoindex(sp('?view=feed')), true);
  assert.equal(wantsNoindex(sp('?view=feed&category=x')), true);
  assert.equal(wantsNoindex(sp('')), false);
  assert.equal(wantsNoindex(sp('?category=government')), false);
  assert.equal(wantsNoindex(sp('?view=list')), false);
  assert.equal(wantsNoindex(sp('?q=abc')), true);
  assert.equal(wantsNoindex(sp('?tag=abc')), true);
  assert.match(readFileSync(new URL('../src/seo.js', import.meta.url), 'utf8'), /if \(wantsNoindex\(url\.searchParams\)\) rw\.on\('head'/);
});
