#!/usr/bin/env node
// サムネイル撮影の作業者（#130）。1 回の実行で 1 サイトを 1 回だけ撮り、結果を 1 行の JSON で標準出力へ出す。
// 呼ぶのは scripts/thumbnail/thumbnail.py だけ。robots.txt・ホスト別の待ち・台帳・画像の変換と保存は Python 側が担当し、
// ここは「ブラウザの中で守ること」だけを受け持つ（試験用の scripts/capture-thumbnails.mjs の検査を踏襲）。
//   node scripts/thumbnail/capture.mjs --url <URL> --out <png> --allow-origins <origin,origin> [--viewport 1280x800]
// 守ること:
//  - 主フレームの遷移（リダイレクト先を含む）は --allow-origins の origin だけ。別サイトは cross-site-redirect、同一サイトで origin が変わるもの
//    （www の有無・http→https）は origin-changed:<origin> で止める（リクエストを送る前に止める。Python 側が遷移先の robots を確認して 1 回だけやり直す）
//  - 内部 IP（名前解決の結果も）へのリクエストは、主フレームもサブリソースも送らない。WebSocket は無効化する
//  - cookie・ログインなし（毎回まっさらなコンテキスト）、JS ダイアログは拒否、ダウンロードは拒否、権限は与えない
//  - ページ全体のリソース上限: リクエスト数・総バイト数の上限を超えたら、以降のリクエストを止める（読めた分で撮る）
//  - タイムアウト（ナビゲーション 20 秒。全体は Python 側が強制終了する）
//  - X-Robots-Tag / <meta name=robots> の noarchive・noimageindex・noindex・none は撮らない
import { createRequire } from 'node:module';
import { readdirSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import net from 'node:net';
import dns from 'node:dns/promises';

const args = process.argv.slice(2);
const opt = name => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const UA = 'MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)';
const NAV_TIMEOUT_MS = 20000;
const SETTLE_MS = 1500;                        // load の後の描画待ち
const MAX_REQUESTS = 150;                      // 1 ページで許すリクエスト数
const MAX_BYTES = 12 * 1024 * 1024;            // 1 ページで許す総転送量
const OBSCURED_RATIO = 0.5;
const BLOCK_TOKENS = ['noarchive', 'noimageindex', 'noindex', 'none'];
const SELFTEST_HOSTS = process.env.MISE_THUMBS_ALLOW_LOCALHOST === '1';   // テスト専用。*.localhost だけ許す
const [VW, VH] = (opt('--viewport') || '1280x800').split('x').map(Number);

export function isUnsafeHost(host) {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase();
  if (h === 'localhost' || h.endsWith('.localhost') || h.endsWith('.local') || h.endsWith('.internal')) return true;
  if (net.isIPv4(h)) {
    const [a, b] = h.split('.').map(Number);
    return a === 0 || a === 10 || a === 127 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127) || a >= 224;
  }
  if (net.isIPv6(h)) return h === '::1' || h === '::' || /^f[cd]/.test(h) || h.startsWith('fe80') || h.startsWith('::ffff:');
  return !h.includes('.');
}
const dnsCache = new Map();
async function hostUnsafe(host) {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase();
  if (SELFTEST_HOSTS && h.endsWith('.localhost')) return false;
  if (isUnsafeHost(h)) return true;
  if (net.isIP(h)) return false;
  if (!dnsCache.has(h)) dnsCache.set(h, dns.lookup(h, { all: true }).then(a => a.some(x => isUnsafeHost(x.address)), () => false));
  return dnsCache.get(h);
}
const sameSite = (a, b) => a.replace(/^www\./, '') === b.replace(/^www\./, '');
const blocked = value => (value || '').toLowerCase().split(/[,\s]+/).filter(t => BLOCK_TOKENS.includes(t));

function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = path.join(homedir(), '.cache/ms-playwright');
  const dirs = existsSync(base) ? readdirSync(base).filter(d => d.startsWith('chromium_headless_shell-')).sort() : [];
  for (const d of dirs.reverse()) for (const sub of readdirSync(path.join(base, d))) {
    const p = path.join(base, d, sub, 'chrome-headless-shell');
    if (existsSync(p)) return p;
  }
  throw new Error('chrome-headless-shell が見つからない。環境変数 CHROMIUM_PATH で指す');
}
function loadPlaywright() {
  const require = createRequire(import.meta.url);
  for (const t of [process.env.PLAYWRIGHT_DIR, 'playwright'].filter(Boolean)) { try { return require(t); } catch { /* 次へ */ } }
  throw new Error('playwright が見つからない。環境変数 PLAYWRIGHT_DIR に playwright パッケージのディレクトリ（node_modules/playwright）を指定する');
}

async function capture(url, out, allowOrigins) {
  const t0 = Date.now();
  const rec = { ok: false, reason: null, http_status: null, final_url: url, obscured_ratio: null, requests: 0, bytes: 0, new_origin: null, elapsed_ms: null };
  const done = reason => { if (reason) rec.reason = reason; rec.elapsed_ms = Date.now() - t0; return rec; };
  const u = new URL(url);
  if (!['http:', 'https:'].includes(u.protocol) || await hostUnsafe(u.hostname)) return done('unsafe-url');
  if (!allowOrigins.has(u.origin)) return done('origin-not-allowed');
  let stop = null;   // ナビゲーション中に違反を見つけた理由
  // 主フレームの遷移先の検査。違反なら理由を返す。
  const checkNav = async nu => {
    if (!['http:', 'https:'].includes(nu.protocol) || await hostUnsafe(nu.hostname)) return 'unsafe-url';
    if (!sameSite(nu.hostname, u.hostname)) return 'cross-site-redirect';
    if (!allowOrigins.has(nu.origin)) { rec.new_origin = nu.origin; return 'origin-changed:' + nu.origin; }
    return null;
  };
  const { chromium } = loadPlaywright();
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
  const browser = await chromium.launch({
    executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu', ...(SELFTEST_HOSTS ? ['--no-proxy-server'] : [])],
    ...(proxy && !SELFTEST_HOSTS ? { proxy: { server: proxy, bypass: 'localhost,127.0.0.1' } } : {}),
  });
  try {
    // 毎回まっさらなコンテキスト（cookie・ストレージ・ログインなし）。ダウンロード不可、権限なし、Service Worker 不可。
    const ctx = await browser.newContext({ userAgent: UA, viewport: { width: VW, height: VH }, deviceScaleFactor: 1, ignoreHTTPSErrors: false, serviceWorkers: 'block', acceptDownloads: false, permissions: [] });
    await ctx.addInitScript(() => { window.WebSocket = function WebSocket() { throw new DOMException('WebSocket is disabled', 'SecurityError'); }; });
    const page = await ctx.newPage();
    page.on('dialog', d => d.dismiss().catch(() => {}));         // alert / confirm / prompt / beforeunload は拒否
    page.on('download', d => d.cancel().catch(() => {}));         // ダウンロードは拒否
    page.on('popup', p => p.close().catch(() => {}));             // 新しいウィンドウは開かせない
    const recheck = async () => { if (stop) return stop; const cu = new URL(page.url()); return cu.protocol.startsWith('http') ? checkNav(cu) : 'error:non-http-page'; };
    const cdp = await ctx.newCDPSession(page);
    const mainFrameId = (await cdp.send('Page.getFrameTree')).frameTree.frame.id;
    await cdp.send('Network.enable');
    cdp.on('Network.loadingFinished', ev => { rec.bytes += ev.encodedDataLength || 0; });
    // 全リクエストを送る前に止めて検査する。リダイレクトは 1 ホップごとに「次のリクエスト」として止まる。
    cdp.on('Fetch.requestPaused', async ev => {
      const id = ev.requestId, fail = (r = 'BlockedByClient') => cdp.send('Fetch.failRequest', { requestId: id, errorReason: r }).catch(() => {});
      try {
        const ru = new URL(ev.request.url);
        if (!['http:', 'https:', 'data:', 'blob:'].includes(ru.protocol)) return fail();
        if (ev.resourceType === 'Document' && ev.frameId === mainFrameId && ru.protocol.startsWith('http')) {
          const bad = await checkNav(ru); if (bad) { stop = stop || bad; rec.final_url = ru.href; return fail(); }
        } else if (ru.protocol.startsWith('http')) {
          if (await hostUnsafe(ru.hostname)) return fail();
          if (++rec.requests > MAX_REQUESTS || rec.bytes > MAX_BYTES) return fail();   // ページ全体のリソース上限
        }
        await cdp.send('Fetch.continueRequest', { requestId: id });
      } catch { fail(); }
    });
    await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] });
    try {
      const resp = await page.goto(url, { waitUntil: 'load', timeout: NAV_TIMEOUT_MS });
      if (!resp) return done(stop || 'no-response');
      rec.http_status = resp.status(); rec.final_url = page.url();
      { const fb = stop || await checkNav(new URL(page.url())); if (fb) return done(fb); }
      if (rec.http_status >= 400) return done('http-' + rec.http_status);
      const xrt = blocked(await resp.headerValue('x-robots-tag'));
      if (xrt.length) return done('x-robots-tag:' + xrt.join('+'));
      await page.waitForTimeout(SETTLE_MS);
      { const lb = await recheck(); if (lb) return done(lb); }   // load 後の meta refresh / JS の遷移
      const meta = await page.evaluate(() => [...document.querySelectorAll('meta[name]')].filter(m => ['robots', 'miseresearchbot'].includes(m.name.toLowerCase())).map(m => m.content).join(','));
      const mb = blocked(meta);
      if (mb.length) return done('meta-robots:' + mb.join('+'));
      // 画面の過半を fixed/sticky 要素が覆っているか（cookie 同意バナー・全面モーダルの決定論検知）。撮り直しはしない。
      rec.obscured_ratio = await page.evaluate(() => {
        const cols = 24, rows = 18; let hit = 0;
        for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
          let el = document.elementFromPoint((c + 0.5) * innerWidth / cols, (r + 0.5) * innerHeight / rows);
          for (; el && el !== document.documentElement; el = el.parentElement) {
            const p = getComputedStyle(el).position;
            if (p === 'fixed' || p === 'sticky') { hit++; break; }
          }
        }
        return Math.round(hit / (cols * rows) * 1000) / 1000;
      });
      { const lb = await recheck(); if (lb) return done(lb); }
      await page.screenshot({ type: 'png', path: out, clip: { x: 0, y: 0, width: VW, height: VH }, timeout: NAV_TIMEOUT_MS });
      rec.ok = true;
      return done();
    } catch (e) {
      if (stop) return done(stop);
      return done(/Timeout/i.test(e.message) ? 'timeout' : 'error:' + e.message.split('\n')[0].slice(0, 200));
    }
  } finally { await browser.close().catch(() => {}); }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const url = opt('--url'), out = opt('--out'), allow = new Set((opt('--allow-origins') || '').split(',').filter(Boolean));
  if (!url || !out || !allow.size) { console.error('usage: capture.mjs --url U --out PNG --allow-origins o1,o2'); process.exit(2); }
  capture(url, out, allow).then(r => { console.log(JSON.stringify(r)); process.exit(0); },
    e => { console.log(JSON.stringify({ ok: false, reason: 'error:' + String(e.message).split('\n')[0].slice(0, 200) })); process.exit(0); });
}
