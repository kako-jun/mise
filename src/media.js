// R2（バインディング MEDIA）に置いた画像の配信。ファビコン(/icon/)と、サムネイル(/thumb/)が同じ仕組みを使う（#125・#130）。
// ファビコン・サムネイルとも、名前はサイト固定（sha256(site_id) の先頭 32 桁）で、更新は同じキーへの上書き（1 サイト 1 枚・世代管理なし。#163）。
// 画像が替わったことは URL の `?v=`（ファビコンは内容ハッシュ、サムネイルは撮影時刻由来）で伝え、v 付きの URL ごとに不変として immutable にする。Worker は query を無視して R2 を引く。
// 過渡期: favicon_v が NULL の旧データは内容ハッシュ名（32 桁 16 進）の /icon/<hash>.webp（v なし）のまま。どちらも同じ形の名前なので同じ経路で引ける。
// MEDIA が無い環境（バケット未作成・バインディング未設定）や、対象が無いとき・名前が不正なときは 404（代替画像は作らない）。
export const MEDIA_KINDS = {
  icon: { prefix: 'icon/', ext: 'webp', type: 'image/webp' },
  thumb: { prefix: 'thumb/', ext: 'webp', type: 'image/webp' }, // サムネイル（#130。幅 320px 前後の横長 1 枚）
};
const HASH = /^[0-9a-f]{32}$/;
const NOT_FOUND = () => new Response('Not Found', { status: 404, headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' } });

// /icon/<hash>.webp の <hash>.webp 部分から R2 のキーを作る。不正なら null。
export function mediaKey(kind, file) {
  const k = MEDIA_KINDS[kind]; if (!k || typeof file !== 'string') return null;
  const m = /^([^.]+)\.([a-z0-9]+)$/.exec(file);
  return m && HASH.test(m[1]) && m[2] === k.ext ? `${k.prefix}${m[1]}.${k.ext}` : null;
}

export async function serveMedia(env, request, kind, file) {
  const key = mediaKey(kind, file);
  if (!key || !env?.MEDIA || typeof env.MEDIA.get !== 'function') return NOT_FOUND();
  const obj = await env.MEDIA.get(key);
  if (!obj) return NOT_FOUND();
  const etag = obj.httpEtag || (obj.etag ? `"${obj.etag}"` : null);
  // v が正しい形（英数字 12 文字まで）のときだけ immutable。v 無し・不正な v でも 200 を返すが、短いキャッシュ（上書きが反映される）。
  const versioned = /^[0-9a-z]{1,12}$/.test(new URL(request.url).searchParams.get('v') ?? '');
  const headers = { 'Content-Type': MEDIA_KINDS[kind].type, 'Cache-Control': versioned ? 'public, max-age=31536000, immutable' : 'public, max-age=3600' };
  if (etag) headers.ETag = etag;
  if (etag && (request.headers.get('If-None-Match') || '').split(',').some(v => v.trim() === '*' || v.trim().replace(/^W\//, '') === etag)) {
    obj.body?.cancel?.();
    return new Response(null, { status: 304, headers });
  }
  return new Response(obj.body, { status: 200, headers });
}

// sites.favicon_path（<32 桁 16 進>.webp。新: サイト固定のキー名 / 旧: 内容ハッシュ名）と favicon_v（表示のバージョン）から配信 URL を作る。形が違う値・NULL は null（何も出さない）。
// favicon_v が正しい形なら `?v=<favicon_v>` を付ける（新データ）。NULL・不正なら v なし（旧データ。従来どおり /icon/<hash>.webp）。
export const faviconUrl = (path, v) => mediaKey('icon', path) ? `/icon/${path}` + (typeof v === 'string' && /^[0-9a-z]{1,12}$/.test(v) ? `?v=${v}` : '') : null;

// sites.thumb_path（サイト固定のキー名 <32 桁 16 進>.webp）と thumb_captured_at（撮影時刻）から配信 URL を作る。形が違う値・NULL は null（何も出さない）。
// `?v=` は撮影時刻のエポック秒の 36 進。撮り直すと変わり、ブラウザは新しい画像を取る。撮影時刻が読めなければ v なし。
export const thumbUrl = (path, capturedAt) => {
  if (!mediaKey('thumb', path)) return null;
  const t = Date.parse(capturedAt);
  return `/thumb/${path}` + (Number.isFinite(t) && t >= 0 ? `?v=${Math.floor(t / 1000).toString(36)}` : '');
};
