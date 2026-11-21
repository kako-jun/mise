#!/usr/bin/env node
// サムネイル撮影の作業者（#130）。1 回の実行で 1 サイトを 1 回だけ撮り、結果を 1 行の JSON で標準出力へ出す。
// 呼ぶのは scripts/thumbnail/thumbnail.py だけ。robots.txt・ホスト別の待ち・台帳・画像の変換と保存・宛先の検査（内部 IP・ポート・名前解決）は Python 側
// （検査付きフィルタリングプロキシ netfilter.py。ブラウザの全通信はそこを通る。アドレス判定の表はそこ 1 か所だけで、ここには持たない）。
// ここは「ブラウザの中で守ること」だけを受け持つ。
//   node scripts/thumbnail/capture.mjs --url <URL> --out <png> --allow-urls <url,url> (--proxy <http://127.0.0.1:port> | --proxy-unix <socket>)
// 経路: Chromium は --proxy-server だけが外への出口（--proxy-bypass-list=<-loopback> でループバックも迂回させない、名前解決は Chromium 自身が行わない、
//   WebRTC の直接 UDP・QUIC は無効）。--proxy-unix のときは、サンドボックス内（ネットワークなし）でループバックの小さな中継を立て、Unix ソケット越しに外のフィルタへつなぐ。
// 守ること:
//  - 主フレームのネットワーク遷移（リダイレクト先を含む）は --allow-urls に挙げた URL だけ。別サイトは cross-site-redirect、同一サイトで未検査の URL
//    （www の有無・http→https・パスの違い）は redirect-unchecked:<完全な URL> で止める（リクエストを送る前に止める。Python 側が遷移先の robots を確認して再実行する）
//  - cookie・ログインなし（毎回まっさらなコンテキスト）、JS ダイアログは拒否、ダウンロードは拒否、ポップアップは閉じる、権限は与えない
//  - X-Robots-Tag / <meta name=robots> の noarchive・noimageindex・noindex・none は撮らない（下の blocked のコメントも参照）
//  - タイムアウト（ナビゲーション 20 秒。全体は Python 側が強制終了する）。受信量・接続数の上限はフィルタ側
import { createRequire } from 'node:module';
import { readdirSync, existsSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { pathToFileURL } from 'node:url';

const args = process.argv.slice(2);
const opt = name => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const UA = 'MiseResearchBot/0.1 (+https://github.com/kako-jun/mise)';
const NAV_TIMEOUT_MS = 20000;
const SETTLE_MS = 1500;                        // load の後の描画待ち
const OBSCURED_RATIO = 0.5;
const BLOCK_TOKENS = ['noarchive', 'noimageindex', 'noindex', 'none'];
const [VW, VH] = (opt('--viewport') || '1280x800').split('x').map(Number);

const sameSite = (a, b) => a.replace(/^www\./, '') === b.replace(/^www\./, '');
// X-Robots-Tag は「bot名: ディレクティブ」の形がありうる。特定の別ボット宛て（例 `googlebot: noindex`）は自分への指示ではないので数えず、
// 宛先なし・MiseResearchBot 宛て・meta の name=robots / miseresearchbot だけを見る（保守的に撮らない側へ倒すのは宛先なしの指示だけ）。
const blocked = value => (value || '').toLowerCase().split(',').flatMap(part => {
  const m = part.match(/^\s*([a-z0-9_-]+)\s*:\s*(.*)$/);
  if (m && !['miseresearchbot'].includes(m[1]) && !BLOCK_TOKENS.includes(m[1])) return [];   // 別ボット宛て
  return (m ? m[2] : part).split(/\s+/).filter(t => BLOCK_TOKENS.includes(t));
});
const norm = u => { const x = new URL(u); x.hash = ''; return x.href; };

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

// ネットワークの無いサンドボックスの中で、127.0.0.1:<空きポート> → Unix ソケットの中継を立てる。戻り: プロキシ URL
function startRelay(socketPath) {
  return new Promise((resolve, reject) => {
    const srv = net.createServer(c => {
      const u = net.connect(socketPath);
      c.pipe(u); u.pipe(c);
      const end = () => { c.destroy(); u.destroy(); };
      c.on('error', end); u.on('error', end); c.on('close', end); u.on('close', end);
    });
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => resolve(`http://127.0.0.1:${srv.address().port}`));
  });
}

async function capture(url, out, allowUrls, proxyUrl) {
  const t0 = Date.now();
  const rec = { ok: false, reason: null, http_status: null, final_url: url, obscured_ratio: null, new_url: null, elapsed_ms: null };
  const done = reason => { if (reason) rec.reason = reason; rec.elapsed_ms = Date.now() - t0; return rec; };
  const u = new URL(url);
  const allowed = new Set(allowUrls.map(norm)), origins = new Set(allowUrls.map(x => new URL(x).origin));
  if (!['http:', 'https:'].includes(u.protocol)) return done('bad_url');
  if (!allowed.has(norm(url))) return done('url-not-allowed');
  let stop = null;   // ナビゲーション中に違反を見つけた理由
  // 遷移先の検査。network=true は実際のネットワーク遷移（パスまで照合）。false は読み込み後の URL（pushState 等でパスが変わることがあるので origin だけ）。
  const checkNav = (nu, network) => {
    if (!['http:', 'https:'].includes(nu.protocol)) return 'bad_url';
    if (!sameSite(nu.hostname, u.hostname)) return 'cross-site-redirect';
    if (network ? !allowed.has(norm(nu.href)) : !origins.has(nu.origin)) { rec.new_url = norm(nu.href); return 'redirect-unchecked:' + rec.new_url; }
    return null;
  };
  const { chromium } = loadPlaywright();
  const browser = await chromium.launch({
    executablePath: chromiumPath(),
    chromiumSandbox: true,   // --no-sandbox を付けない（レビューで bwrap 内でも動くと確認）
    args: [
      `--proxy-server=${proxyUrl}`, '--proxy-bypass-list=<-loopback>',   // 外への出口はフィルタだけ。ループバック宛ても迂回させずフィルタに通す
      '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1',        // ブラウザ自身は名前解決しない（フィルタが解決して検査する）
      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp', '--disable-quic', '--no-pings', '--disable-gpu',
    ],
  });
  try {
    // 毎回まっさらなコンテキスト（cookie・ストレージ・ログインなし）。ダウンロード不可、権限なし、Service Worker 不可。
    const ctx = await browser.newContext({ userAgent: UA, viewport: { width: VW, height: VH }, deviceScaleFactor: 1, ignoreHTTPSErrors: false, serviceWorkers: 'block', acceptDownloads: false, permissions: [] });
    const page = await ctx.newPage();
    page.on('dialog', d => d.dismiss().catch(() => {}));         // alert / confirm / prompt / beforeunload は拒否
    page.on('download', d => d.cancel().catch(() => {}));         // ダウンロードは拒否
    page.on('popup', p => p.close().catch(() => {}));             // 新しいウィンドウは閉じる（通信はフィルタを通るので内部アドレスへは出られない）
    const recheck = () => { if (stop) return stop; const cu = new URL(page.url()); return cu.protocol.startsWith('http') ? checkNav(cu, false) : 'error:non-http-page'; };
    // 主フレームのドキュメント遷移を、送る前に止めて検査する（リダイレクトは 1 ホップごとに「次のリクエスト」として止まる）。それ以外の通信はフィルタが検査する。
    const cdp = await ctx.newCDPSession(page);
    const mainFrameId = (await cdp.send('Page.getFrameTree')).frameTree.frame.id;
    cdp.on('Fetch.requestPaused', async ev => {
      const id = ev.requestId, fail = (r = 'BlockedByClient') => cdp.send('Fetch.failRequest', { requestId: id, errorReason: r }).catch(() => {});
      try {
        const ru = new URL(ev.request.url);
        if (!['http:', 'https:', 'data:', 'blob:'].includes(ru.protocol)) return fail();
        if (ev.resourceType === 'Document' && ev.frameId === mainFrameId && ru.protocol.startsWith('http')) {
          const bad = checkNav(ru, true); if (bad) { stop = stop || bad; rec.final_url = ru.href; return fail(); }
        }
        await cdp.send('Fetch.continueRequest', { requestId: id });
      } catch { fail(); }
    });
    await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] });
    try {
      const resp = await page.goto(url, { waitUntil: 'load', timeout: NAV_TIMEOUT_MS });
      if (!resp) return done(stop || 'no-response');
      rec.http_status = resp.status(); rec.final_url = page.url();
      { const fb = stop || checkNav(new URL(page.url()), false); if (fb) return done(fb); }
      if (rec.http_status >= 400) return done('http-' + rec.http_status);
      const xrt = blocked(await resp.headerValue('x-robots-tag'));
      if (xrt.length) return done('x-robots-tag:' + xrt.join('+'));
      await page.waitForTimeout(SETTLE_MS);
      { const lb = recheck(); if (lb) return done(lb); }   // load 後の meta refresh / JS の遷移
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
      { const lb = recheck(); if (lb) return done(lb); }
      await page.screenshot({ type: 'png', path: out, clip: { x: 0, y: 0, width: VW, height: VH }, timeout: NAV_TIMEOUT_MS });
      rec.ok = true;
      return done();
    } catch (e) {
      if (stop) return done(stop);
      return done(/Timeout/i.test(e.message) ? 'timeout' : 'error:' + e.message.split('\n')[0].slice(0, 200));
    }
  } finally { await browser.close().catch(() => {}); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const url = opt('--url'), out = opt('--out'), allow = (opt('--allow-urls') || '').split(',').filter(Boolean);
  const unix = opt('--proxy-unix'), direct = opt('--proxy');
  if (!url || !out || !allow.length || !(unix || direct)) { console.error('usage: capture.mjs --url U --out PNG --allow-urls u1,u2 (--proxy URL | --proxy-unix SOCK)'); process.exit(2); }
  (async () => {
    const proxyUrl = unix ? await startRelay(unix) : direct;
    return capture(url, out, allow, proxyUrl);
  })().then(r => { console.log(JSON.stringify(r)); process.exit(0); },
    e => { console.log(JSON.stringify({ ok: false, reason: 'error:' + String(e.message).split('\n')[0].slice(0, 200) })); process.exit(0); });
}
