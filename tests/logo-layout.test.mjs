import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { inlineScriptHashes } from '../src/security.js';

// ロゴの横伸ばし・文字間(#152)。実際のインク測定は実ブラウザ(scripts/e2e-logo-ink.mjs)で行い、ここでは配置の計算と配線を検査する。
const html = readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
const logoScript = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]).find((s) => s.includes('window.__miseLogoLayout'));

test('ロゴのインラインスクリプト: sha256 が CSP 用ハッシュ一覧に入る', async () => {
  assert.ok(logoScript);
  const want = createHash('sha256').update(logoScript).digest('base64');
  assert.ok((await inlineScriptHashes(html)).includes(want));
});

const run = (extra = {}) => {
  const ctx = { window: {}, ...extra };
  ctx.window = ctx; // window.__x = ... がグローバルに載る
  vm.createContext(ctx);
  vm.runInContext(logoScript.slice(logoScript.indexOf('// logo-script:begin'), logoScript.indexOf('// logo-script:end')), ctx); // マーカーの間(関数定義だけ。末尾の fonts 待ちは含めない)
  return ctx;
};

// 長方形のインク(行数 H)。left=インク左端、right=インク右端。
const box = (left, right, H = 10) => ({ left: Array(H).fill(left), right: Array(H).fill(right) });

test('__miseLogoLayout: 平均インク間隔が G にそろい、インク全体が中央に来る', () => {
  const { __miseLogoLayout: layout } = run();
  const A = [30, 12, 28, 26], prof = [box(2, 28), box(1, 11), box(3, 25), box(2, 24)], sc = [1.5, 1, 1.4, 1], G = 17;
  const mg = layout(prof, A, sc, G);
  assert.equal(mg.length, 4);
  // 配置を再現して間隔を検算(transform は span 中央基準)
  let x = mg[0][0]; const P = [], X = [];
  for (let k = 0; k < 4; k++) { if (k) x += mg[k][0]; P.push(x); x += A[k] + mg[k][1]; }
  const ink = (k) => [P[k] + A[k] / 2 + (prof[k].left[0] - A[k] / 2) * sc[k], P[k] + A[k] / 2 + (prof[k].right[0] - A[k] / 2) * sc[k]];
  for (let k = 0; k < 3; k++) assert.ok(Math.abs(ink(k + 1)[0] - ink(k)[1] - G) < 1e-9);
  const total = x; // 行の全幅
  const l = ink(0)[0], r = total - ink(3)[1];
  assert.ok(Math.abs(l - r) < 1e-9, `左右の余白 ${l} / ${r}`);
});

test('__miseLogoLayout: 曲線の引っ込み(行ごとに端が違う)は平均で扱う', () => {
  const { __miseLogoLayout: layout } = run();
  const A = [20, 20, 20, 20], I = box(0, 20, 4);
  const S = { left: [4, 0, 0, 4], right: [16, 20, 20, 16] }; // 上下が引っ込んだ S
  const mg = layout([I, I, S, I], A, [1, 1, 1, 1], 17);
  // I-S の平均間隔 = 平均(4,0,0,4)=2 → I の右端 20 と S の左端の差が 17 になるよう margin は 15
  assert.ok(Math.abs(mg[2][0] - 15) < 1e-9, String(mg[2][0]));
});

const fakeEnv = (widths, fontFail = false) => {
  const spans = ['M', 'I', 'S', 'E'].map((t, k) => ({ textContent: t, style: {}, getBoundingClientRect: () => ({ width: widths[k] }) }));
  const classes = new Set();
  const g = {
    clearRect() {}, fillText() {}, get font() { return fontFail ? '10px sans-serif' : this._f || ''; }, set font(v) { this._f = v; }, set textBaseline(v) {}, set fillStyle(v) {},
    getImageData: (x, y, w, h) => { const d = new Uint8ClampedArray(w * h * 4); for (let j = 0; j < h; j++) for (let i = 8; i < w - 8; i++) d[(j * w + i) * 4 + 3] = j % 3 ? 255 : 0; return { data: d }; },
  };
  return {
    spans, classes,
    document: {
      querySelectorAll: () => spans,
      createElement: () => ({ getContext: () => g }),
      documentElement: { classList: { add: (c) => classes.add(c) } },
    },
    getComputedStyle: () => ({ fontSize: '54px', fontWeight: '1000', fontFamily: 'Mise Logo', fontStretch: '75%' }),
  };
};
const snap = (e) => JSON.stringify(e.spans.map((s) => [s.style.transform, s.style.margin]));

test('__miseLogo: 倍率は M 1.5 / I 1 / S 1.4 / E 1 で、再実行しても同じ結果(冪等)', () => {
  const e = fakeEnv([30, 12, 28, 26]);
  const ctx = run(e);
  ctx.__miseLogo();
  assert.deepEqual(e.spans.map((s) => s.style.transform), ['scaleX(1.5)', 'scaleX(1)', 'scaleX(1.4)', 'scaleX(1)']);
  assert.ok(e.classes.has('logo-ready'));
  const first = snap(e);
  ctx.__miseLogo(); ctx.__miseLogo();
  assert.equal(snap(e), first);
});

test('__miseLogo: フォント未読込(幅 0)・要素不足でも落ちず、logo-ready を付けない(CSS の遅延アニメが表示する)', () => {
  const e = fakeEnv([0, 0, 0, 0]);
  assert.doesNotThrow(() => run(e).__miseLogo());
  assert.ok(!e.classes.has('logo-ready'));
  const e2 = fakeEnv([30, 12, 28, 26]); e2.document.querySelectorAll = () => e2.spans.slice(0, 2);
  assert.doesNotThrow(() => run(e2).__miseLogo());
  assert.ok(!e2.classes.has('logo-ready'));
  const e3 = fakeEnv([30, 12, 28, 26]); e3.document.createElement = () => { throw new Error('canvas 不可'); };
  assert.doesNotThrow(() => run(e3).__miseLogo());
});

test('__miseLogo: 成功済みの状態から測定が失敗しても、前の配置に戻す(伸ばしが外れない)', () => {
  const e = fakeEnv([30, 12, 28, 26]), ctx = run(e);
  ctx.__miseLogo();
  const ok = snap(e);
  e.document.createElement = () => { throw new Error('canvas 不可'); };
  ctx.__miseLogo();
  assert.equal(snap(e), ok);
  const e2 = fakeEnv([30, 12, 28, 26]), ctx2 = run(e2); ctx2.__miseLogo(); const ok2 = snap(e2);
  e2.spans.forEach((s) => { s.getBoundingClientRect = () => ({ width: 0 }); }); // 幅 0 の早期 return
  ctx2.__miseLogo();
  assert.equal(snap(e2), ok2);
});

test('__miseLogo: 初回の失敗では inline を空のまま残す(style.css のフォールバックが効く)', () => {
  for (const mk of [() => fakeEnv([0, 0, 0, 0]), () => { const e = fakeEnv([30, 12, 28, 26]); e.document.createElement = () => { throw new Error('x'); }; return e; }]) {
    const e = mk(); run(e).__miseLogo();
    assert.deepEqual(e.spans.map((s) => [s.style.transform, s.style.margin]), e.spans.map(() => [undefined, undefined]));
    assert.ok(!e.classes.has('logo-ready'));
  }
});

test('__miseLogo: canvas の font 設定が効かない(既定の 10px sans-serif に落ちる)とき測定せず戻す', () => {
  const e = fakeEnv([30, 12, 28, 26], true);
  run(e).__miseLogo();
  assert.deepEqual(e.spans.map((s) => [s.style.transform, s.style.margin]), e.spans.map(() => [undefined, undefined]));
  assert.ok(!e.classes.has('logo-ready'));
});
