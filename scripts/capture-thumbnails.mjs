#!/usr/bin/env node
// サムネイル撮影の試験 (issue #41 段階3)。設計・作法・限界は docs/thumbnails.md。
// 実行: node scripts/capture-thumbnails.mjs [--only id,id] [--limit N] [--out var/thumbs]
// 自己検査 (実サイトへアクセスしない): node scripts/capture-thumbnails.mjs --selftest
// 画像実体 (var/thumbs/*.webp) はコミットしない。実行のたびに実サイトへアクセスする。
import { spawn, spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdirSync, readFileSync, writeFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import net from 'node:net';
import dns from 'node:dns/promises';
import http from 'node:http';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
function opt0(name) { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; }
const UA = 'MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)';
const UA_TOKEN = 'miseresearchbot';
const NAV_TIMEOUT_MS = 20000;
const SETTLE_MS = 1500; // load 後の描画待ち
const SELFTEST = args.includes('--selftest');
const GAP_MS = SELFTEST ? 0 : 2000; // サイト間(robots取得含め全リクエストの間)の最小間隔
const VIEWPORT = { width: 1280, height: 800 }; // PC画面の最初の画面。横長(16:10)のまま縮めて1枚にする
const MOBILE_VIEWPORT = { width: 390, height: 844 }; // 比較用。dsf=1、UAは変えず viewport のみ
const MOBILE_W = Number(opt0('--mobile-width') || 195);
const MOBILE_IDS = (opt0('--mobile') || '').split(',').filter(Boolean); // 比較用スマホ画面も撮るサイトid (all で全件)
const THUMB_W = Number(opt0('--width') || 320); // 採用値。理由は docs/thumbnails.md
const THUMB_H = Math.round(THUMB_W * VIEWPORT.height / VIEWPORT.width);
const WEBP_QUALITY = opt0('--quality') || '40';
const COMPARE_WIDTHS = (opt0('--compare') || '').split(',').filter(Boolean).map(Number); // 例 240,320,400: 比較用を var/thumbs/compare/<幅>/ にも出す
const PLACEHOLDER_W = 16;
const OBSCURED_RATIO = 0.5;
const BLOCK_TOKENS = ['noarchive', 'noimageindex', 'noindex', 'none'];

const opt = opt0;
const OUT = path.resolve(ROOT, opt('--out') || 'var/thumbs');
const ONLY = opt('--only')?.split(',');
const LIMIT = opt('--limit') ? Number(opt('--limit')) : Infinity;

// headless chromium は GPU デバイスを開く所 (drm_open) で固まることがあるため、/dev/dri を隠す bwrap の中で自分自身を再実行する。
if (!process.env.MISE_THUMBS_IN_BWRAP && !args.includes('--no-bwrap')) {
  const r = spawnSync('bwrap', ['--dev-bind', '/', '/', '--tmpfs', '/dev/dri', process.execPath, ...process.argv.slice(1)],
    { stdio: 'inherit', env: { ...process.env, MISE_THUMBS_IN_BWRAP: '1' } });
  if (r.error) { console.error('bwrap を実行できない (' + r.error.code + ')。GPUデバイスで固まらない環境なら --no-bwrap を付けて再実行する。'); process.exit(1); }
  process.exit(r.status ?? 1);
}

// ---- 対象: embeddability.json の25件 + migrations の 0014 (米国12件) ----
function loadSites() {
  const sites = JSON.parse(readFileSync(path.join(ROOT, 'data/embeddability.json'), 'utf8')).sites
    .map((s) => ({ id: s.id, url: s.url }));
  const sql = readFileSync(path.join(ROOT, 'migrations/0014_us_batch1.sql'), 'utf8');
  for (const m of sql.matchAll(/INTO sites \([^)]*\) VALUES \('([^']+)','([^']+)'/g)) sites.push({ id: m[1], url: m[2] });
  const seen = new Set();
  return sites.filter((s) => !seen.has(s.id) && seen.add(s.id));
}

// ---- 作法 ----
function isUnsafeHost(host) {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase();
  if (h === 'localhost' || h.endsWith('.localhost') || h.endsWith('.local') || h.endsWith('.internal')) return true;
  if (net.isIPv4(h)) {
    const [a, b] = h.split('.').map(Number);
    return a === 0 || a === 10 || a === 127 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127) || a >= 224;
  }
  if (net.isIPv6(h)) return h === '::1' || h === '::' || /^f[cd]/.test(h) || h.startsWith('fe80') || h.startsWith('::ffff:');
  return !h.includes('.');
}
// 自己検査中だけ、*.localhost を許可する (リテラルIPは自己検査でも拒否する)。
const selftestHost = (h) => SELFTEST && h.endsWith('.localhost');
const dnsCache = new Map();
// ホスト名が内部IPに解決されるものを含め、危険なら true。名前解決の失敗は false (ブラウザ側で失敗する)。
async function hostUnsafe(host) {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase();
  if (selftestHost(h)) return false;
  if (isUnsafeHost(h)) return true;
  if (net.isIP(h)) return false;
  if (!dnsCache.has(h)) {
    dnsCache.set(h, dns.lookup(h, { all: true }).then((a) => a.some((x) => isUnsafeHost(x.address)), () => false));
  }
  return dnsCache.get(h);
}
const sameSite = (a, b) => a.replace(/^www\./, '') === b.replace(/^www\./, '');

function curlRobots(origin) { // 非同期 (自己検査ではローカルサーバーが同じプロセスにいるため、同期実行だと固まる)
  const o = new URL(origin);
  const extra = SELFTEST ? ['--noproxy', '*', '--resolve', `${o.hostname}:${o.port}:127.0.0.1`] : [];
  return new Promise((resolve) => {
    const c = spawn('curl', ['-sS', '-w', '\n%{http_code}', '--max-time', '15', '--max-filesize', '524288', ...extra, '-A', UA, `${origin}/robots.txt`]);
    let out = '', err = ''; c.stdout.on('data', (d) => { out += d; }); c.stderr.on('data', (d) => { err += d; });
    c.on('close', (code) => {
      if (code !== 0) return resolve({ error: (err || 'curl-failed').trim().slice(0, 120) });
      const i = out.lastIndexOf('\n'); resolve({ status: Number(out.slice(i + 1)), body: out.slice(0, i) });
    });
  });
}

// 最小の robots.txt 解釈: 該当UAのグループがあればそれ、なければ * 。最長一致、同長なら Allow 優先。
// パーセントエンコードの正規化: 非予約文字は復号、それ以外は16進を大文字に。
const normPct = (p) => p.replace(/%([0-9a-f]{2})/gi, (m, h) => { const c = String.fromCharCode(parseInt(h, 16)); return /[A-Za-z0-9\-._~]/.test(c) ? c : '%' + h.toUpperCase(); });
function robotsAllows(body, pathAndQuery) {
  pathAndQuery = normPct(pathAndQuery);
  const groups = []; let cur = null; let lastWasRule = true;
  for (const raw of body.split(/\r?\n/)) {
    const line = raw.replace(/#.*/, '').trim(); const m = line.match(/^([A-Za-z-]+)\s*:\s*(.*)$/); if (!m) continue;
    const k = m[1].toLowerCase(), v = m[2].trim();
    if (k === 'user-agent') { if (!cur || lastWasRule) { cur = { uas: [], rules: [] }; groups.push(cur); } cur.uas.push(v.toLowerCase()); lastWasRule = false; }
    else if ((k === 'allow' || k === 'disallow') && cur) { cur.rules.push([k, v]); lastWasRule = true; }
  }
  const g = groups.filter((x) => x.uas.some((u) => u !== '' && u !== '*' && UA_TOKEN.includes(u)))
  const use = g.length ? g : groups.filter((x) => x.uas.includes('*'));
  let best = { len: -1, allow: true };
  for (const grp of use) for (const [k, v] of grp.rules) {
    if (!v) continue;
    const re = new RegExp('^' + normPct(v).replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\\\$$/, '$'));
    if (re.test(pathAndQuery)) {
      const len = v.length, allow = k === 'allow';
      if (len > best.len || (len === best.len && allow)) best = { len, allow };
    }
  }
  return best.allow;
}

const blocked = (value) => (value || '').toLowerCase().split(/[,\s]+/).filter((t) => BLOCK_TOKENS.includes(t));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let lastReqAt = 0;
async function gap() { const w = lastReqAt + GAP_MS - Date.now(); if (w > 0) await sleep(w); lastReqAt = Date.now(); }
// robots.txt を確認して、撮ってよければ null、だめなら理由を返す。
async function robotsReason(u) {
  await gap();
  const rb = await curlRobots(u.origin);
  if (rb.error) return 'robots-unknown';
  if (rb.status === 200) return robotsAllows(rb.body, u.pathname + u.search) ? null : 'robots-disallow';
  return rb.status === 404 || rb.status === 410 ? null : 'robots-unknown';
}

function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const base = path.join(homedir(), '.cache/ms-playwright');
  const dirs = existsSync(base) ? readdirSync(base).filter((d) => d.startsWith('chromium_headless_shell-')).sort() : [];
  for (const d of dirs.reverse()) {
    for (const sub of readdirSync(path.join(base, d))) {
      const p = path.join(base, d, sub, 'chrome-headless-shell');
      if (existsSync(p)) return p;
    }
  }
  throw new Error('chrome-headless-shell が見つからない。CHROMIUM_PATH を指定する');
}

function toWebp(png, file, w = THUMB_W, extra = []) {
  const r = spawnSync('magick', ['png:-', '-resize', `${w}x`,
    '-quality', WEBP_QUALITY, '-define', 'webp:method=6', ...extra, `webp:${file}`], { input: png });
  if (r.status !== 0) throw new Error('webp-encode-failed');
}
// 読み込み用の極小プレースホルダ (幅16px、data URI)。
function placeholder(png) {
  const r = spawnSync('magick', ['png:-', '-resize', `${PLACEHOLDER_W}x`, '-quality', '30', '-define', 'webp:method=6', 'webp:-'], { input: png });
  if (r.status !== 0) throw new Error('placeholder-failed');
  return 'data:image/webp;base64,' + r.stdout.toString('base64');
}

async function capture(browser, site, outDir = OUT) {
  const t0 = Date.now();
  const rec = { id: site.id, url: site.url, fetched_at_utc: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), ok: false, reason: null, width: null, height: null, placeholder: null, http_status: null, final_url: null, bytes: null, obscured: null, obscured_ratio: null, elapsed_ms: null };
  const done = (reason) => { if (reason) rec.reason = reason; rec.elapsed_ms = Date.now() - t0; return rec; };
  const u = new URL(site.url);
  if (!['http:', 'https:'].includes(u.protocol) || await hostUnsafe(u.hostname)) return done('unsafe-url');
  const r0 = await robotsReason(u);
  if (r0) return done(r0);
  const okOrigins = new Set([u.origin]); // robots.txt を確認済みの origin
  let stop = null; // ナビゲーション中に違反を見つけた理由
  // ナビゲーション先 (リダイレクト先を含む) の検査。違反なら理由を返す。
  const checkNav = async (nu) => {
    if (!['http:', 'https:'].includes(nu.protocol) || await hostUnsafe(nu.hostname)) return 'unsafe-url';
    if (!sameSite(nu.hostname, u.hostname)) return 'cross-site-redirect';
    if (!okOrigins.has(nu.origin)) { const r = await robotsReason(nu); if (r) return r; okOrigins.add(nu.origin); }
    return null;
  };
  await gap();

  const ctx = await browser.newContext({ userAgent: UA, viewport: VIEWPORT, deviceScaleFactor: 1, ignoreHTTPSErrors: false, serviceWorkers: 'block' });
  const page = await ctx.newPage();
  // CDP の Fetch ドメインで全リクエストを送信前に止めて検査する。リダイレクトは1ホップごとに「次のリクエスト」として
  // 止まるので、別サイト・内部IPへのリクエストはそもそも送られない (playwright の route はリダイレクト後を捕捉しない)。
  const cdp = await ctx.newCDPSession(page);
  const mainFrameId = (await cdp.send('Page.getFrameTree')).frameTree.frame.id;
  cdp.on('Fetch.requestPaused', async (ev) => {
    const id = ev.requestId; const fail = (r = 'BlockedByClient') => cdp.send('Fetch.failRequest', { requestId: id, errorReason: r }).catch(() => {});
    try {
      const ru = new URL(ev.request.url);
      if (!['http:', 'https:', 'data:', 'blob:'].includes(ru.protocol)) return fail();
      if (ev.resourceType === 'Document' && ev.frameId === mainFrameId && ru.protocol.startsWith('http') && !process.env.MISE_THUMBS_GUARD_OFF) {
        const bad = await checkNav(ru); if (bad) { stop = stop || bad; rec.final_url = ru.href; return fail(); }
      } else if (ru.protocol.startsWith('http') && await hostUnsafe(ru.hostname)) return fail();
      await cdp.send('Fetch.continueRequest', { requestId: id });
    } catch { fail(); }
  });
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] });
  try {
    rec.final_url = site.url;
    const resp = await page.goto(site.url, { waitUntil: 'load', timeout: NAV_TIMEOUT_MS });
    if (!resp) return done('no-response');
    rec.http_status = resp.status(); rec.final_url = page.url();
    // 保険: 最終URLも検査する (ここに来る前にルートが止めているはず)
    { const fb = stop || await checkNav(new URL(page.url())); if (fb) return done(fb); }
    if (rec.http_status >= 400) return done('http-' + rec.http_status);
    const xrt = blocked(await resp.headerValue('x-robots-tag'));
    if (xrt.length) return done('x-robots-tag:' + xrt.join('+'));
    await page.waitForTimeout(SETTLE_MS);
    const meta = await page.evaluate(() => [...document.querySelectorAll('meta[name]')].filter((m) => ['robots', 'miseresearchbot'].includes(m.name.toLowerCase())).map((m) => m.content).join(','));
    const mb = blocked(meta);
    if (mb.length) return done('meta-robots:' + mb.join('+'));
    // 画面の過半を fixed/sticky 要素が覆っているか (cookie 同意バナー等の決定論検知)。撮り直しはしない。
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
    rec.obscured = rec.obscured_ratio > OBSCURED_RATIO;
    const png = await page.screenshot({ type: 'png', clip: { x: 0, y: 0, ...VIEWPORT }, timeout: NAV_TIMEOUT_MS });
    const file = path.join(outDir, `${site.id}.webp`);
    toWebp(png, file);
    rec.bytes = statSync(file).size; rec.width = THUMB_W; rec.height = THUMB_H; rec.placeholder = placeholder(png); rec.ok = true;
    if (MOBILE_IDS.includes('all') || MOBILE_IDS.includes(site.id)) {
      // 比較用: 同じ読み込み済みページの viewport だけを狭める (再リクエストしない = 1サイト1回を守る)
      await page.setViewportSize(MOBILE_VIEWPORT); await page.waitForTimeout(1000);
      const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: innerWidth }));
      const mpng = await page.screenshot({ type: 'png', clip: { x: 0, y: 0, ...MOBILE_VIEWPORT }, timeout: NAV_TIMEOUT_MS });
      const md = path.join(outDir, 'mobile'); mkdirSync(md, { recursive: true });
      const mf = path.join(md, `${site.id}.webp`); toWebp(mpng, mf, MOBILE_W);
      rec.mobile = { viewport: MOBILE_VIEWPORT, width: MOBILE_W, height: Math.round(MOBILE_W * MOBILE_VIEWPORT.height / MOBILE_VIEWPORT.width), bytes: statSync(mf).size, scroll_width: m.sw, h_overflow: m.sw > m.iw + 1 };
    }
    for (const w of COMPARE_WIDTHS) { const d = path.join(outDir, 'compare', String(w)); mkdirSync(d, { recursive: true }); toWebp(png, path.join(d, `${site.id}.webp`), w); }
    return done();
  } catch (e) {
    if (stop) return done(stop);
    return done(/Timeout/i.test(e.message) ? 'timeout' : 'error:' + e.message.split('\n')[0].slice(0, 300));
  } finally { await ctx.close(); }
}

function loadPlaywright() {
  const require = createRequire(import.meta.url);
  const tries = [process.env.PLAYWRIGHT_DIR, 'playwright'].filter(Boolean);
  for (const t of tries) { try { return require(t); } catch { /* 次へ */ } }
  throw new Error('playwright が見つからない。環境変数 PLAYWRIGHT_DIR に playwright パッケージのディレクトリ (node_modules/playwright) を指定するか、このリポに入れる (npm i -D playwright はしない方針なので、既存のインストールを指す)。');
}

// ---- 自己検査: ローカルの小さなサーバーだけを相手に、リダイレクトの検査を確かめる (実サイトへアクセスしない) ----
async function selftest() {
  const log = [];
  const html = '<!doctype html><title>t</title><body style="background:#4a8"><h1>selftest</h1>';
  const server = http.createServer((req, res) => {
    const host = (req.headers.host || '').split(':')[0]; log.push(`${host}${req.url}`);
    const P = server.address().port;
    const go = (loc) => { res.writeHead(302, { location: loc }); res.end(); };
    if (req.url === '/robots.txt') {
      if (host === 'www.c.localhost') { res.writeHead(200, { 'content-type': 'text/plain' }); return res.end('User-agent: *\nDisallow: /\n'); }
      res.writeHead(404); return res.end();
    }
    if (req.url === '/ok') { res.writeHead(200, { 'content-type': 'text/html' }); return res.end(html); }
    if (req.url === '/redir-other') return go(`http://b.localhost:${P}/ok`);
    if (req.url === '/redir-ip') return go(`http://127.0.0.1:${P}/ok`);
    if (req.url === '/redir-www') return go(`http://www.${host}:${P}/ok`);
    if (req.url === '/redir-chain') return go(`http://www.${host}:${P}/redir-other`);
    res.writeHead(404); res.end();
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const P = server.address().port;
  const cases = [ // [id, url, 期待する reason (null = 撮影成功)]
    ['st-ok', `http://a.localhost:${P}/ok`, null],
    ['st-other-site', `http://a.localhost:${P}/redir-other`, 'cross-site-redirect'],
    ['st-literal-ip', `http://a.localhost:${P}/redir-ip`, 'unsafe-url'],
    ['st-www', `http://a.localhost:${P}/redir-www`, null],
    ['st-www-robots-disallow', `http://c.localhost:${P}/redir-www`, 'robots-disallow'],
    ['st-chain-to-other-site', `http://d.localhost:${P}/redir-chain`, 'cross-site-redirect'],
    ['st-literal-ip-site', `http://127.0.0.1:${P}/ok`, 'unsafe-url'],
  ];
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu', '--no-proxy-server'] });
  const tmp = mkdtempSync(path.join(tmpdir(), 'mise-thumbs-selftest-'));
  let bad = 0;
  try {
    for (const [id, url, want] of cases) {
      const r = await capture(browser, { id, url }, tmp);
      const got = r.ok ? null : r.reason;
      const pass = got === want; if (!pass) bad++;
      console.error(`${pass ? 'PASS' : 'FAIL'}\t${id}\twant=${want}\tgot=${got}\tfinal=${r.final_url}`);
    }
  } finally { await browser.close(); server.close(); }
  // 別サイト・リテラルIP・robots不許可サイトの本体には、一度もリクエストが届いていないこと
  for (const forbidden of ['b.localhost/ok', '127.0.0.1/ok', 'www.c.localhost/ok', 'b.localhost/robots.txt']) {
    const hit = log.includes(forbidden); if (hit) bad++;
    console.error(`${hit ? 'FAIL' : 'PASS'}\tサーバーに ${forbidden} が届いていない`);
  }
  const wr = log.includes('www.a.localhost/robots.txt'); if (!wr) bad++;
  console.error(`${wr ? 'PASS' : 'FAIL'}\t同一サイト内の遷移先 (www) の robots.txt を確認した`);
  console.error(bad ? `selftest: ${bad} 件失敗` : 'selftest: all passed');
  process.exit(bad ? 1 : 0);
}

async function main() {
  if (spawnSync('magick', ['-version']).error) throw new Error('ImageMagick (magick) が見つからない。WebP対応版を入れる。');
  if (SELFTEST) return selftest();
  const { chromium } = loadPlaywright();
  mkdirSync(OUT, { recursive: true });
  let sites = loadSites().filter((s) => !ONLY || ONLY.includes(s.id)).slice(0, LIMIT);
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
  const browser = await chromium.launch({ executablePath: chromiumPath(), args: ['--no-sandbox', '--disable-gpu'], ...(proxy ? { proxy: { server: proxy, bypass: 'localhost,127.0.0.1' } } : {}) });
  const results = [];
  try {
    for (const s of sites) {
      const r = await capture(browser, s); results.push(r);
      console.error(`${s.id}\t${r.ok ? 'ok ' + r.bytes + 'B' : r.reason}\t${r.elapsed_ms}ms${r.obscured ? '\tobscured' : ''}`);
      await sleep(GAP_MS);
    }
  } finally { await browser.close(); }
  writeFileSync(path.join(OUT, 'manifest.json'), JSON.stringify({ ua: UA, viewport: VIEWPORT, thumb: [THUMB_W, THUMB_H], width: THUMB_W, height: THUMB_H, webp_quality: Number(WEBP_QUALITY), obscured_threshold: OBSCURED_RATIO, sites: results }, null, 1) + '\n');
}
main().catch((e) => { console.error(e); process.exit(1); });
