#!/usr/bin/env node
// capture.mjs の自己検査（実サイトへアクセスしない。ローカルの小さなサーバーの *.localhost だけが相手）。要 playwright・chrome-headless-shell。
//   bwrap --dev-bind / / --tmpfs /dev/dri node scripts/thumbnail/selftest.mjs
// 確かめること: 別サイト・リテラル IP へのリダイレクト／meta refresh／JS の遷移・同一サイト内の origin 変更（許可なし = origin-changed、許可あり = 撮れる）・
// noindex の meta・X-Robots-Tag・404・ダイアログ・ダウンロード・リソース上限・WebSocket・内部 IP のサブリソース。サーバーに「届いてはいけないリクエスト」が来ていないこと。
import http from 'node:http';
import { execFile } from 'node:child_process';
import { mkdtempSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const CAPTURE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'capture.mjs');
const log = [];
const imgs = Array.from({ length: 400 }, (_, i) => `<img width=1 height=1 src="/img/${i}.png">`).join('');
const server = http.createServer((req, res) => {
  const host = (req.headers.host || '').split(':')[0]; log.push(`${host}${req.url}`);
  const P = server.address().port, html = (b, h = {}) => { res.writeHead(200, { 'content-type': 'text/html', ...h }); res.end(`<!doctype html><title>t</title><body style="background:#4a8"><h1>${b}</h1>${b === 'limit' ? imgs : ''}`); };
  const go = loc => { res.writeHead(302, { location: loc }); res.end(); };
  const u = req.url;
  if (u.startsWith('/img/')) { res.writeHead(200, { 'content-type': 'image/png' }); return res.end(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64')); }
  if (u === '/ok') return html('ok');
  if (u === '/redir-other') return go(`http://b.localhost:${P}/ok`);
  if (u === '/redir-ip') return go(`http://127.0.0.1:${P}/ok`);
  if (u === '/redir-www') return go(`http://www.${host}:${P}/ok`);
  if (u === '/meta') return html(`<meta http-equiv="refresh" content="0;url=http://b.localhost:${P}/ok">meta`);
  if (u === '/js') return html(`js<script>setTimeout(()=>{location.href='http://b.localhost:${P}/ok'},100)</script>`);
  if (u === '/ws') return html(`ws<script>try{new WebSocket('ws://127.0.0.1:${P}/wsprobe')}catch(e){}</script>`);
  if (u === '/sub-ip') return html(`sub<img src="http://127.0.0.1:${P}/subprobe.png"><script src="http://127.0.0.1:${P}/subprobe.js"></script>`);
  if (u === '/noindex') return html('<meta name="robots" content="noindex">noindex');
  if (u === '/xrt') return html('xrt', { 'x-robots-tag': 'noarchive' });
  if (u === '/404') { res.writeHead(404, { 'content-type': 'text/html' }); return res.end('<h1>not found</h1>'); }
  if (u === '/dialog') return html('dialog<script>alert("a");confirm("c");prompt("p")</script>');
  if (u === '/download') { res.writeHead(200, { 'content-type': 'application/octet-stream', 'content-disposition': 'attachment; filename=x.bin' }); return res.end('x'); }
  if (u === '/limit') return html('limit');
  res.writeHead(404); res.end();
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const P = server.address().port, tmp = mkdtempSync(path.join(tmpdir(), 'mise-thumbs-selftest-'));
// [名前, host, path, 許可する origin(host)の追加, 期待する reason(null = 撮れる)]
const cases = [
  ['ok', 'a', '/ok', [], null],
  ['other-site', 'a', '/redir-other', [], 'cross-site-redirect'],
  ['literal-ip', 'a', '/redir-ip', [], 'unsafe-url'],
  ['www-not-allowed', 'a', '/redir-www', [], 'origin-changed:http://www.a.localhost:' + P],
  ['www-allowed', 'a', '/redir-www', ['www.a'], null],
  ['meta-refresh', 'a', '/meta', [], 'cross-site-redirect'],
  ['js-location', 'a', '/js', [], 'cross-site-redirect'],
  ['websocket', 'a', '/ws', [], null],
  ['sub-ip', 'a', '/sub-ip', [], null],
  ['noindex', 'a', '/noindex', [], 'meta-robots:noindex'],
  ['x-robots-tag', 'a', '/xrt', [], 'x-robots-tag:noarchive'],
  ['404', 'a', '/404', [], 'http-404'],
  ['dialog', 'a', '/dialog', [], null],
  ['download', 'a', '/download', [], 'NOT_OK'],
  ['limit', 'a', '/limit', [], null],
  ['literal-ip-site', '127.0.0.1', '/ok', [], 'unsafe-url'],
];
let bad = 0;
// 撮影プロセスは非同期で呼ぶ（同期だとこのプロセスのサーバーが応答できなくなる）
const runAsync = (host, p, extra) => new Promise(resolve => {
  const url = `http://${host === '127.0.0.1' ? host : host + '.localhost'}:${P}${p}`;
  const allow = [new URL(url).origin, ...extra.map(h => `http://${h}.localhost:${P}`)];
  const out = path.join(tmp, `x-${Math.random().toString(36).slice(2)}.png`);
  execFile(process.execPath, [CAPTURE, '--url', url, '--out', out, '--allow-origins', allow.join(',')], { env: { ...process.env, MISE_THUMBS_ALLOW_LOCALHOST: '1' }, timeout: 60000 }, (err, stdout, stderr) => {
    try { resolve({ ...JSON.parse(stdout.trim().split('\n').pop()), png: existsSync(out) }); } catch { resolve({ ok: false, reason: 'no-json:' + String(stderr).slice(0, 120) }); }
  });
});
for (const [name, host, p, extra, want] of cases) {
  const r = await runAsync(host, p, extra);
  const got = r.ok ? null : r.reason;
  const pass = want === 'NOT_OK' ? !r.ok : got === want;
  if (!pass) bad++;
  console.error(`${pass ? 'PASS' : 'FAIL'}\t${name}\twant=${want}\tgot=${got}${r.png && !r.ok ? '\t(png が残っている)' : ''}`);
  if (!r.ok && r.png) bad++;
}
for (const f of ['b.localhost/ok', '127.0.0.1/ok', '127.0.0.1/wsprobe', '127.0.0.1/subprobe.png', '127.0.0.1/subprobe.js']) { const hit = log.includes(f); if (hit) bad++; console.error(`${hit ? 'FAIL' : 'PASS'}\tサーバーに ${f} が届いていない`); }
const imgHits = log.filter(l => l.includes('/img/')).length;
const lim = imgHits <= 160; if (!lim) bad++;
console.error(`${lim ? 'PASS' : 'FAIL'}\tリソース上限: 400 枚の画像のうちサーバーに届いたのは ${imgHits} 件（上限 150 前後）`);
console.error(bad ? `selftest: ${bad} 件失敗` : 'selftest: all passed');
server.close(); process.exit(bad ? 1 : 0);
