import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
// DOM を持たない環境のため、#118 の契約をソースの不変条件として固定する（実挙動は実ブラウザで確認。docs/design/ui.md）。
const read = p => readFileSync(new URL(`../${p}`, import.meta.url), 'utf8');
const index = read('public/index.html'), about = read('public/about.html'), app = read('public/app.js'), css = read('public/style.css'), worker = read('src/index.js');
test('フッタ: 「i」リンクは作者リンク列の最左・/about/ へ同一タブ', () => {
  const nav = index.match(/<nav class="author-links"[\s\S]*?<\/nav>/)[0];
  const links = [...nav.matchAll(/<a href="([^"]+)"([^>]*)>/g)];
  assert.equal(links.length, 3);
  assert.equal(links[0][1], '/about/');
  assert.doesNotMatch(links[0][2], /target=/);
  assert.match(links[0][2], /aria-label="About MISE" title="About MISE" data-i18n-label="about"/);
  assert.match(nav.slice(0, nav.indexOf('>')), /aria-label="Links" data-i18n-label="linksLabel"/); // 作者に限らない中立の語
  assert.match(app, /linksLabel:'リンク'/);
  assert.match(app, /linksLabel:'Links'/);
  assert.doesNotMatch(app, /authorLabel/);
  assert.match(app, /about:'MISE について'/);
  assert.match(app, /about:'About MISE'/);
  assert.match(css, /\.author-links a \{ width: 32px; height: 32px/);
});
test('/about/: Worker が静的 about.html を返し、/about は 301、範囲ルートより先に登録', () => {
  assert.match(worker, /app\.get\('\/about\/'/);
  assert.match(worker, /app\.get\('\/about'.*301/);
  assert.ok(worker.indexOf("'/about/'") < worker.indexOf("'/:scope/'"));
});
test('/about/: 日英の本文・出典へのリンク・canonical', () => {
  assert.match(about, /<h1>About MISE<\/h1>/);
  assert.match(about, /<h1>MISE について<\/h1>/);
  assert.match(about, /class="t-en" lang="en"/);
  assert.match(about, /class="t-ja" lang="ja"/);
  assert.match(about, /href="\/licenses\/public-data-credits\.txt"/);
  assert.match(about, /rel="canonical" href="https:\/\/mise\.llll-ll\.com\/about\/"/);
  assert.doesNotMatch(about, /公共データ利用規約/); // 出典文そのものは画面に再掲しない（#111）
});
test('/about/: 官公庁の基準と並びの但し書き、JS 前は設定ボタンを隠す', () => {
  assert.match(about, /独立した 2 つ以上の出典でホスト名が一致/);
  assert.match(about, /at least two independent sources/);
  assert.match(about, /登録日で代用/);
  assert.match(about, /<div class="preferences" hidden>/);
  assert.match(read('public/about.js'), /\.preferences'\)\.hidden = false/);
  assert.match(read('public/about.css'), /\.preferences\[hidden\] \{ display: none; \}/);
});
