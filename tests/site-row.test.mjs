// 一覧の行（#166）: ファビコンはサイト名の左だけ・枠なし／サイト名より下は常に左端／リンク先ホスト名の表示。
// DOM の実測は実ブラウザで行う（PR に記録）。ここでは表示ホスト名の変換と、構造・CSS の取り決めを静的に守らせる。
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const read = f => readFileSync(new URL('../' + f, import.meta.url), 'utf8');
const hostJs = read('public/site-host.js'), css = read('public/style.css'), app = read('public/app.js');
const ctx = { URL, String, Error }; ctx.globalThis = ctx; vm.runInNewContext(hostJs, ctx);
const siteHost = ctx.MiseSiteHost;

test('siteHost: href と同じ URL のホスト名だけ（パス・クエリ・ポート・資格情報は出さない。www. はそのまま）', () => {
  assert.equal(siteHost('https://kinpusen.or.jp/'), 'kinpusen.or.jp');
  assert.equal(siteHost('https://www.example.com/a/b?q=1#x'), 'www.example.com');
  assert.equal(siteHost('http://example.com:8080/'), 'example.com');
  assert.equal(siteHost('https://user:pw@example.com/'), 'example.com');
  assert.equal(siteHost('https://EXAMPLE.com/'), 'example.com');
  assert.equal(siteHost('https://example.com/blog/'), 'example.com'); // サブディレクトリのサイトでもホスト名だけ
});

test('siteHost: 国際化ドメインは Unicode に戻す', () => {
  assert.equal(siteHost('https://日本語.jp/'), '日本語.jp');
  assert.equal(siteHost('https://例え.テスト/'), '例え.テスト');
  assert.equal(siteHost('https://xn--r8jz45g.xn--zckzah/'), '例え.テスト');
  assert.equal(siteHost('https://日本語-abc.co.jp/'), '日本語-abc.co.jp'); // 漢字+ラテン+ハイフンの混在は可
  assert.equal(siteHost('https://한국.example/'), '한국.example'); // ラテンと紛らわしくない 1 文字体系
  assert.equal(siteHost('https://xn--wgv71a119e.jp/'), '日本語.jp');
  assert.equal(siteHost('https://bücher.example/'), 'bücher.example');
  assert.equal(siteHost('https://xn--bcher-kva.example/'), 'bücher.example');
  assert.doesNotThrow(() => siteHost('https://xn--a-.example/')); // 壊れた punycode でも例外にしない
});

test('siteHost: 偽装になりうる IDN は punycode のまま（ホスト名全体）', () => {
  assert.equal(siteHost('https://xn--80ak6aa92e.com/'), 'xn--80ak6aa92e.com'); // キリル文字の apple 偽装
  assert.equal(siteHost('https://аррӏе.com/'), 'xn--80ak6aa92e.com');
  assert.equal(siteHost('https://www.xn--80ak6aa92e.com/'), 'www.xn--80ak6aa92e.com'); // 安全なラベルが混じっても全体を戻さない
  assert.equal(siteHost('https://pаypal.com/'), new URL('https://pаypal.com/').hostname); // キリル a がラテンに混在
  assert.match(siteHost('https://pаypal.com/'), /^xn--/);
  assert.match(siteHost('https://ηλ.example/'), /^xn--/); // ギリシャ
  assert.match(siteHost('https://日本語.xn--80ak6aa92e.com/'), /^日本語\.xn--|^xn--/); // 偽装ラベルを 1 つでも含めば全体を戻さない
  assert.equal(siteHost('https://日本語.xn--80ak6aa92e.com/'), 'xn--wgv71a119e.xn--80ak6aa92e.com');
  assert.equal(siteHost('https://ｅｘａｍｐｌｅ.com/'), 'example.com'); // 全角英数は URL の正規化で ASCII になる
});

test('siteHost: 不正・空・http(s) 以外は null（何も出さない）', () => {
  for (const v of [null, undefined, '', 'not a url', '/relative', 'javascript:alert(1)', 'data:text/html,x', 'ftp://example.com/']) assert.equal(siteHost(v), null, String(v));
});

test('行の構造: ファビコンは 1 行目（.site-head）のサイト名の左。説明文・タグ・所属は .site-content 直下（インデントしない）', () => {
  assert.match(app, /head\.append\(bullet,icon,title\)/);
  assert.match(app, /content\.append\(head\)/);
  assert.doesNotMatch(app, /li\.append\(icon/);
  assert.match(app, /li\.append\(content\)/);
  assert.match(app, /img\.width=16; img\.height=16/);
  assert.match(app, /icon\.setAttribute\('aria-hidden','true'\)/);
  assert.match(app, /img\.alt=''/);
  assert.match(app, /img\.addEventListener\('error',\(\)=>\{icon\.hidden=true/);
});

const rule = sel => { const out = []; for (const m of css.matchAll(/^([^{}\n]+)\{([^}]*)\}/gm)) if (m[1].split(',').map(s => s.trim()).includes(sel)) out.push(m[2]); return out.join(' '); };

test('CSS: .site-icon とその画像に枠・角丸・縁・下地を付けない（ダーク用の下地も無い）', () => {
  for (const sel of ['.site-icon', '.site-icon img']) {
    const r = rule(sel);
    assert.match(r, /border:\s*0/); assert.match(r, /border-radius:\s*0/); assert.match(r, /box-shadow:\s*none/); assert.match(r, /background:\s*none/);
  }
  assert.doesNotMatch(css, /html\[data-theme="dark"\] \.site-icon/);
  assert.doesNotMatch(css, /\.site-icon[^{]*\{[^}]*(?:rgba\(|#[0-9a-f]{3,6})/i);
  assert.match(rule('.site-icon'), /width:\s*16px/);
});

test('CSS: 行頭の中点は li::before（フォント依存）でなく、1 行目の flex 内の要素を文字のベースライン基準（align-items:baseline + 大文字高の半分 .357em だけ上げる）にそろえる（#183）', () => {
  assert.doesNotMatch(css, /(?:#directory-sites|#result-list) li::before/);
  assert.match(rule('.site-head'), /display:\s*flex/); assert.match(rule('.site-head'), /align-items:\s*baseline/); assert.match(rule('.site-bullet'), /translateY\(calc\(2\.5px - \.357em\)\)/); assert.match(rule('.site-icon'), /align-items:\s*center/); assert.match(rule('.site-icon'), /translateY\(calc\(8px - \.357em\)\)/);
  assert.doesNotMatch(css, /(?:#directory-sites|#result-list|#pick-list) li \{[^}]*display:\s*flex/);
});

test('ホスト名: サイト名の直後（同じ行）に 1 つだけ。aria-hidden にしない。案 B・切替は無い', () => {
  assert.doesNotMatch(css, /data-url-layout|site-host-below/); assert.doesNotMatch(app, /urllayout|urlLayout|site-host-below/);
  assert.equal((app.match(/className='site-host'/g) || []).length, 1);
  assert.match(rule('.site-host'), /overflow-wrap:\s*anywhere/);
  assert.doesNotMatch(rule('.site-host'), /break-all/); // 短いホスト名を文字単位に割らない
  assert.match(app, /globalThis\.MiseSiteHost \? globalThis\.MiseSiteHost\(value\) : null/); // site-host.js が無くても一覧は出す
  assert.match(app, /const host=siteHost\(site\.url\)/);
  assert.doesNotMatch(app, /site-host[^;\n]*aria-hidden/);
});

test('CSS: 行の高さは文字サイズに追従する（px 固定にしない）。サイト名の line-height も単位なし', () => {
  assert.match(rule('.site-head'), /line-height:\s*1\.46\b(?!\s*px)/);
  assert.doesNotMatch(css, /\.site-title[^{]*\{[^}]*line-height:\s*\d+px/);
  assert.match(rule('.site-bullet'), /height:\s*5px/); assert.match(rule('.site-icon'), /height:\s*16px/); // 中点・ファビコンの位置は em（.357em）で文字サイズに追従する
});

test('1 行目の並び: 名前・新規タブ・NEW・国旗・ホスト名', () => {
  const i = app.indexOf('function renderSite');
  const body = app.slice(i, app.indexOf('target.append(li)', i));
  const pos = k => body.indexOf(k);
  assert.ok(pos('newTabButton(href,site.name)') < pos("tag.textContent=' NEW'") && pos("tag.textContent=' NEW'") < pos('title.append(flag)') && pos('title.append(flag)') < pos("hs.className='site-host'"));
});

test('SSR: 一覧の行の構造はブラウザ描画と同じ（中点・サイト名・ホスト名、説明文は次の行）。ホスト名は 1 回だけ計算', () => {
  const seo = read('src/seo.js');
  assert.match(seo, /class="site-head"><span class="site-bullet" aria-hidden="true">/);
  assert.match(seo, /<p>\$\{s\.description_en/);
  assert.equal((seo.match(/MiseSiteHost\(/g) || []).length, 1);
});

test('縦フィード: カードにホスト名を 1 行（共有の site-host.js を使い、無くても動く）', () => {
  const feed = read('public/feed.js');
  assert.match(feed, /globalThis\.MiseSiteHost && globalThis\.MiseSiteHost\(s\.url\)/);
  assert.match(feed, /hs\.className = 'f-host'/);
  assert.match(read('public/feed.css'), /#feed \.f-host/);
});

// カテゴリ見出し行（#182）: サイト行と同じ構造（中点・16px 枠のフォルダ絵・本文）。SSR とクライアントで同じクラス構成。
test('カテゴリ見出し行: 中点とフォルダ絵（mask・currentColor・装飾）がサイト行と同じ構造で、SSR と app.js が一致する', () => {
  const seo = read('src/seo.js');
  const ssr = seo.match(/<div class="category\$\{[^}]*\}[^`]*?<\/h2><\/div>/)[0];
  assert.match(ssr, /<div class="site-head"><span class="site-bullet" aria-hidden="true"><\/span><span class="site-icon cat-icon" aria-hidden="true"><\/span><h2>/);
  assert.match(app, /hd\.className='site-head'/);
  assert.match(app, /bl\.className='site-bullet'; bl\.setAttribute\('aria-hidden','true'\)/);
  assert.match(app, /ci\.className='site-icon cat-icon'; ci\.setAttribute\('aria-hidden','true'\)/);
  assert.match(app, /hd\.append\(bl,ci,h\)/);
  assert.match(css, /\.site-icon\.cat-icon \{[^}]*background: currentColor;/);
  assert.match(css, /\.site-icon\.cat-icon \{[^}]*-webkit-mask: url\("data:image\/svg\+xml/);
  assert.match(css, /\.site-icon\.cat-icon \{[^}]*(?<!-webkit-)mask: url\("data:image\/svg\+xml/); // 無印の mask も別に検査（-webkit- に当たらせない）
  assert.match(css, /@media \(forced-colors: active\) \{[^}]*\.site-icon\.cat-icon[^}]*\.site-bullet::before[^}]*background: CanvasText;[^}]*forced-color-adjust: none;/);
  assert.doesNotMatch(css, /\.category::before/); // 旧い文字の中点は持たない
  assert.doesNotMatch(css, /\.category \{[^}]*padding-left/);
});
