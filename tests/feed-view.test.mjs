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
  for (const ok of ['/thumb/a.webp', '/thumb/a.webp?v=1x2y', '/x/y.png']) assert.equal(lib.thumbOf(ok, O), ok, ok);
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

// フックを疑似 document で実行する。scripts は head に積まれたもの、timers は setTimeout の予約
function runHook() {
  const hook = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).find(s => s.includes('feed.js'));
  const classes = new Set(), head = [], timers = [];
  const el = tag => ({ tag, remove() { this.removed = true; } });
  const document = { documentElement: { classList: { add: c => classes.add(c), remove: c => classes.delete(c) } }, head: { append: e => head.push(e) }, createElement: el, getElementById: () => null };
  vm.runInNewContext(hook, { URLSearchParams, document, location: { search: '?view=feed' }, setTimeout: (fn, ms) => timers.push({ fn, ms }) });
  return { classes, head, timers };
}

test('フック: 読み込みに失敗(onerror)・起動しない(タイムアウト)ときは feed-mode を外して通常表示に戻す', () => {
  const a = runHook();
  assert.ok(a.classes.has('feed-mode'));
  const scripts = a.head.filter(e => e.tag === 'script'); assert.equal(scripts.length, 2);
  scripts[1].onerror();
  assert.ok(!a.classes.has('feed-mode'), 'feed.js の失敗');
  assert.ok(a.head.find(e => e.tag === 'link').removed, 'feed.css も外す');
  const b = runHook(); b.head.filter(e => e.tag === 'script')[0].onerror();
  assert.ok(!b.classes.has('feed-mode'), 'feed-lib.js の失敗');
  const c = runHook(); assert.equal(c.timers.length, 1); assert.equal(c.timers[0].ms, 10000);
  c.timers[0].fn();
  assert.ok(!c.classes.has('feed-mode'), '起動しないまま 10 秒');
});

test('isPending: 要求した範囲を含む読み込みが無い間は true（古い範囲のデータを絞り込んで出さない）', () => {
  assert.equal(lib.isPending(null, false, 'all'), true, '最初の読み込み前');
  assert.equal(lib.isPending('jp', true, 'us'), true, 'jp を読んでいる最中に us へ切替');
  assert.equal(lib.isPending('all', false, 'us'), true, '上限で切れた全世界は us を含むとは言えない');
  assert.equal(lib.isPending('all', true, 'us'), false, '全件読めた全世界なら絞り込むだけ');
  assert.equal(lib.isPending('jp', false, 'jp'), false);
  // feed.js は pending の間、絞り込み直し・言語切替の描画・注記を保留する
  assert.match(appJs, /function refilter\(\) \{ syncUrl\(\); renderBar\(\); if \(pending\(\)\) return;/);
});

test('fmtCount: 表示言語に合わせた桁区切り。数でない値は NaN と出さず空文字', () => {
  assert.equal(lib.fmtCount(101868, 'en'), '101,868');
  assert.equal(lib.fmtCount(101868, 'ja'), '101,868');
  assert.equal(lib.fmtCount(0, 'ja'), '0');
  for (const bad of [undefined, null, NaN, '', 'x']) assert.equal(lib.fmtCount(bad, 'ja'), '', String(bad));
  assert.match(appJs, /String\(n\)\.padStart\(6, '0'\)/, 'SEEN の固定桁は変えない');
});

test('カテゴリ欄: 選択中のカテゴリはその範囲で 0 件でも常に選択肢に出す', () => {
  assert.match(appJs, /if \(n \|\| c\.id === state\.cat\) sel\.append/);
});

test('「最初に戻る」「並びを変えてもう一度」は先頭カードへフォーカスを移し、連続操作の基準も 0 にする', () => {
  assert.match(appJs, /top\.addEventListener\('click', \(\) => \{ navTarget = 0;.*const c = go\(0\); if \(c\) c\.focus\(\{ preventScroll: true \}\); \}\);/);
  assert.match(appJs, /renderList\(\); const c = go\(0, true\); if \(c\) c\.focus\(\{ preventScroll: true \}\); \}\); row\.append\(re\);/);
});

test('feed.js: feed-mode が無い（フックが通常表示へ戻した後に届いた）ときは #feed を作らず keydown も奪わない', () => {
  const calls = { created: 0, listeners: [], appended: 0, fetched: 0 };
  const doc = { readyState: 'complete', documentElement: { classList: { contains: () => false } },
    createElement: () => { calls.created++; return {}; }, body: { append: () => calls.appended++ }, addEventListener: (...a) => calls.listeners.push(a[0]), querySelector: () => null };
  const ctx = { document: doc, MiseFeedLib: lib, matchMedia: () => ({ matches: false }), navigator: {}, location: { search: '?view=feed', pathname: '/', origin: 'https://x.example' },
    URLSearchParams, URL, Date, Math, JSON, Number, Object, Array, String, Map, Set, Promise, setTimeout, clearTimeout, fetch: () => { calls.fetched++; return Promise.reject(Error('no')); },
    addEventListener: (...a) => calls.listeners.push(a[0]), ResizeObserver: class {}, IntersectionObserver: class {}, CustomEvent: class {} };
  ctx.window = ctx;
  vm.runInNewContext(appJs, ctx);
  assert.deepEqual(calls.listeners, [], 'window/document にリスナーを足さない');
  assert.equal(calls.created, 0); assert.equal(calls.appended, 0); assert.equal(calls.fetched, 0);
});

// サムネのパン(#173)
test('panGeometry: 見切れ幅が 8px 以上のときだけ移動量を返す(境界・枠より狭い・縦長・0/NaN)', () => {
  const g = lib.panGeometry;
  // 枠 390x445 に 320x200(1.6:1)→ 画像の実幅 712、見切れ 322、片側 161
  assert.deepEqual({ ...g(390, 445, 320, 200) }, { w: 712, px: 161 });
  assert.deepEqual({ ...g(100, 100, 108, 100) }, { w: 108, px: 4 }, 'ちょうど 8px は動かす');
  assert.equal(g(100, 100, 107.99, 100), null, '8px 未満は動かさない');
  assert.equal(g(500, 100, 160, 100), null, '画像が枠より狭い(枠幅 500 > 160)');
  assert.equal(g(400, 400, 100, 200), null, '縦長画像(幅が枠に届かない)');
  for (const bad of [[0, 100, 320, 200], [100, 0, 320, 200], [100, 100, 0, 200], [100, 100, 320, 0], [NaN, 100, 320, 200], [100, 100, 320, NaN], [100, 100, Infinity, 200], [-1, 100, 320, 200], ['100', 100, 320, 200]]) assert.equal(g(...bad), null, JSON.stringify(bad));
});

test('feed.css: パンは prefers-reduced-motion: no-preference の中だけ。離れたカードは paused で止めた位置に留め、表示中(.is-active)だけ running', () => {
  const uses = [...css.matchAll(/animation\s*:[^;}]*f-pan[^;}]*/g)];
  assert.equal(uses.length, 1, 'f-pan を使う animation は 1 か所');
  const at = css.indexOf(uses[0][0]);
  const media = css.lastIndexOf('@media', at), open = css.indexOf('{', media), close = (() => { let d = 0; for (let i = open; i < css.length; i++) { if (css[i] === '{') d++; else if (css[i] === '}' && --d === 0) return i; } return -1; })();
  assert.ok(media >= 0 && at > open && at < close, '@media の中');
  assert.match(css.slice(media, open), /prefers-reduced-motion:\s*no-preference/);
  const inner = css.slice(open, close);
  assert.match(inner, /#feed \.f-hero\s*>\s*img\[data-pan\]\s*\{[^}]*animation-play-state:\s*paused/, '基本は paused(消すと translate が 0 に戻って飛ぶ)');
  assert.match(inner, /#feed \.f-card\.is-active \.f-hero\s*>\s*img\[data-pan\]\s*\{\s*animation-play-state:\s*running/, '.is-active のときだけ running');
  assert.ok(!/\.is-active[^{]*\{[^}]*animation\s*:/.test(inner), '.is-active 側で animation 自体は付け外ししない');
  // 入場時に飛ばない: 半周期ぶんの負の delay で中央から始める
  assert.match(uses[0][0], /ease-in-out\s+calc\(var\(--pan-sec\)\s*\/\s*-2\)\s+infinite\s+alternate/, '負の animation-delay で中央から始める');
  const kf = css.match(/@keyframes f-pan\s*\{([\s\S]*?\})\s*\}/);
  assert.ok(kf, 'keyframes がある');
  assert.ok(!/transform/.test(kf[1]) && /translate/.test(kf[1]), 'keyframes 全体が translate だけ(transform を重ねない)');
});

test('feed.js: 寸法が変わったら、表示中でないカードの data-pan と --pan-w/--pan-px を消してから表示中のカードだけ測り直す', () => {
  const ro = appJs.match(/new ResizeObserver\(\(\) => \{([^}]*)\}\)\.observe\(scroller\)/);
  assert.ok(ro, 'scroller の ResizeObserver がある');
  assert.match(ro[1], /clearIdlePan\(\);\s*panFit\(activeHeroes\(\)\)/, '解除→表示中だけ再計算の順');
  const fn = appJs.match(/function clearIdlePan\(\) \{([^\n]*)\}\n/);
  assert.ok(fn, 'clearIdlePan がある');
  assert.match(fn[1], /\.f-card:not\(\.is-active\) \.f-hero/);
  assert.match(fn[1], /delete el\.dataset\.pan/);
  assert.match(fn[1], /removeProperty\('--pan-w'\)/);
  assert.match(fn[1], /removeProperty\('--pan-px'\)/);
  assert.ok(!/clientWidth|clientHeight|getBoundingClientRect|offsetWidth|offsetHeight/.test(fn[1]), '読まない(強制レイアウトを起こさない)');
});
