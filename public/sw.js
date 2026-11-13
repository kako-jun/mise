// 最小の service worker。キャッシュは一切作らない（docs/design/architecture.md「PWA」）。
// 役目は、ページ移動(navigate)がネットワーク失敗したときに「オフラインです」を出すことだけ。
// HTML・API・静的ファイルは常にネットワークから取り、古いカテゴリ/サイトを現行と誤認させない。
const OFFLINE_HTML = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex"><title>MISE — Offline</title><style>body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;font:16px system-ui,sans-serif;color:#000;background:#fff;text-align:center}h1{margin:0;font-size:20px;color:#c90018}p{margin:0}@media(prefers-color-scheme:dark){body{background:#07141c;color:#effaff}}</style></head><body><h1>You are offline / オフラインです</h1><p>MISE needs a network connection. / MISE はネットワークに接続すると使えます。</p><p><a href="/">Reload / 再読み込み</a></p></body></html>';
// この応答だけは SW が作るので CSP も自分で付ける（script は許可せず、style はこの応答内のインラインだけ許可）。
const OFFLINE_HEADERS = {
  'Content-Type': 'text/html; charset=utf-8',
  'Cache-Control': 'no-store',
  'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
  'X-Content-Type-Options': 'nosniff',
};
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil((async () => {
  // 過去にキャッシュを作る版があっても残さない。
  for (const k of await caches.keys()) await caches.delete(k);
  // 起動時の SW 待ちでナビゲーションが遅れないよう preload を使う（キャッシュではなくネットワーク応答そのもの）
  if (self.registration.navigationPreload) await self.registration.navigationPreload.enable();
  await self.clients.claim();
})()));
self.addEventListener('fetch', e => {
  if (e.request.mode !== 'navigate') return; // それ以外は SW を通さず通常のネットワーク処理
  e.respondWith((async () => {
    try { return (await e.preloadResponse) || await fetch(e.request); }
    catch { return new Response(OFFLINE_HTML, { status: 503, headers: OFFLINE_HEADERS }); }
  })());
});
