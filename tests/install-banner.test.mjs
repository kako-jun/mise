import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
// 抑制判定の純関数(public/install.js)は実挙動で、DOM 配線(app.js・index.html・style.css)は不変条件で検査する(#105)。
const read = p => readFileSync(new URL(`../${p}`, import.meta.url), 'utf8');
const listeners = {}, fired = [];
const win = { addEventListener: (n, f) => { listeners[n] = f; }, dispatchEvent: e => { fired.push(e.type); } };
vm.runInNewContext(read('public/install.js'), { window: win, Event: class { constructor(type) { this.type = type; } } });
const { DISMISS_MS, parseDismissedAt, isDismissActive } = win.MiseInstall;
const installSrc = read('public/install.js'), app = read('public/app.js'), html = read('public/index.html'), css = read('public/style.css');
const NOW = 1_800_000_000_000;
test('抑制判定: 未却下は出す・7 日以内は抑制・ちょうど 7 日以降は出す', () => {
  assert.equal(DISMISS_MS, 7 * 24 * 60 * 60 * 1000);
  assert.equal(isDismissActive(null, NOW), false);
  assert.equal(isDismissActive(NOW - 1000, NOW), true);
  assert.equal(isDismissActive(NOW - DISMISS_MS + 1, NOW), true);
  assert.equal(isDismissActive(NOW - DISMISS_MS, NOW), false);
  assert.equal(isDismissActive(NOW - DISMISS_MS - 1, NOW), false);
});
test('抑制判定: 未来時刻(時計が進んだ後に戻った場合など)は抑制しない', () => {
  assert.equal(isDismissActive(NOW + 60_000, NOW), false);
  assert.equal(isDismissActive(NOW + DISMISS_MS * 10, NOW), false);
  assert.equal(isDismissActive(NOW, NOW), true);
});
test('捕捉: beforeinstallprompt を止めて window.MiseInstall.event に保持し通知する', () => {
  let prevented = false; const ev = { preventDefault() { prevented = true; } };
  assert.equal(win.MiseInstall.event, null);
  listeners.beforeinstallprompt(ev);
  assert.equal(prevented, true); assert.equal(win.MiseInstall.event, ev); assert.deepEqual(fired, ['miseinstallprompt']);
});
test('保存値の解釈: 数字列だけを受け付け、壊れた値・未保存は null', () => {
  assert.equal(parseDismissedAt(String(NOW)), NOW);
  for (const bad of [null, undefined, '', 'abc', '-5', '1.5', '0', '12e3', ' 5', '9'.repeat(20), 5]) assert.equal(parseDismissedAt(bad), null, String(bad));
});
test('配線: 標準提案を止め・standalone と却下中は出さず・appinstalled で消す', () => {
  assert.match(installSrc, /addEventListener\('beforeinstallprompt', e => \{\s*e\.preventDefault\(\);\s*api\.event = e;/);
  assert.doesNotMatch(app, /addEventListener\('beforeinstallprompt'/);
  assert.match(app, /box\.hidden = !\(mi && mi\.event && !isStandalone\(\) && !installDismissed\(\)\)/);
  assert.match(app, /matchMedia\('\(display-mode: standalone\)'\)\.matches/);
  assert.match(app, /addEventListener\('appinstalled', \(\) => closeInstallBanner\(false\)\)/);
});
test('配線: [あとで]は記憶して閉じ、インストールは prompt() し dismissed なら記憶する', () => {
  assert.match(app, /\$\('#install-later'\)\.addEventListener\('click', \(\) => closeInstallBanner\(true\)\)/);
  assert.match(app, /await ev\.prompt\(\); dismissed = \(await ev\.userChoice\)\.outcome === 'dismissed'/);
  assert.match(app, /if \(remember\) storage\.set\(INSTALL_DISMISS_KEY, String\(Date\.now\(\)\)\)/); // storage は try/catch 済み
});
test('文言: 日英の両方に 3 キー・言語切替で再描画', () => {
  for (const lang of ['ja', 'en']) {
    const line = app.split('\n').find(l => l.startsWith(`Object.assign(words.${lang}, { installText:`));
    assert.ok(line, lang); for (const k of ['installText', 'installAdd', 'installLater']) assert.match(line, new RegExp(`${k}:'`));
  }
  assert.match(app, /renderLog\(\);\s*renderInstallBanner\(\);/);
});
test('マークアップ・CSS: install.js が app.js より先・既定 hidden・[hidden] 併記・下部固定・safe-area', () => {
  assert.ok(html.indexOf('/install.js') > 0 && html.indexOf('/install.js') < html.indexOf('/app.js'));
  assert.match(html, /id="install-banner"[^>]*\bhidden\b/);
  assert.match(css, /\.install-banner\[hidden\] \{ display: none; \}/);
  const rule = css.match(/\.install-banner \{[^}]*\}/)[0];
  assert.match(rule, /position: fixed/); assert.match(rule, /bottom: calc\(12px \+ env\(safe-area-inset-bottom\)\)/);
  assert.doesNotMatch(rule, /\btop:/);
});
test('実行順: インストール関連の宣言は起動処理(applyStaticLanguage を同期で呼ぶ即時関数)より前にある(TDZ で起動が止まらない)', () => {
  const boot = app.indexOf('(async()=>{\n  try {\n    currentScope=scopeFromLocation();');
  assert.ok(boot > 0, '起動処理が見つかる');
  for (const decl of ['const INSTALL_DISMISS_KEY', 'const installDismissed', 'const isStandalone', 'function renderInstallBanner']) {
    const at = app.indexOf(decl); assert.ok(at > 0 && at < boot, `${decl} は起動処理より前`);
  }
  assert.doesNotMatch(app, /\blet installEvent\b/);
  assert.match(app.slice(boot), /window\.__miseBooted = true/); // 起動完了の印(e2e の待ち条件)
});
test('install.js が無くても(window.MiseInstall なし)例外にしない: 参照はすべてガード付き', () => {
  const rest = app.replace('if (window.MiseInstall) window.MiseInstall.event = null', '').replace('window.MiseInstall && window.MiseInstall.event', '');
  assert.doesNotMatch(rest, /window\.MiseInstall\./, '直接の参照が残っている');
  assert.match(app, /const installDismissed = \(\) => \{ const mi = window\.MiseInstall; return !!mi && /);
});
