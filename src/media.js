// R2（バインディング MEDIA）に置いた画像の配信。ファビコン(/icon/)と、サムネイル(/thumb/)が同じ仕組みを使う（#125・#130）。
// 画像の名前は内容ハッシュ（32 桁 16 進）なので中身は変わらない。immutable の長期キャッシュにできる。
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
  const headers = { 'Content-Type': MEDIA_KINDS[kind].type, 'Cache-Control': 'public, max-age=31536000, immutable' };
  if (etag) headers.ETag = etag;
  if (etag && (request.headers.get('If-None-Match') || '').split(',').some(v => v.trim() === '*' || v.trim().replace(/^W\//, '') === etag)) {
    obj.body?.cancel?.();
    return new Response(null, { status: 304, headers });
  }
  return new Response(obj.body, { status: 200, headers });
}

// sites.favicon_path（ハッシュ名 <hash>.webp）から配信 URL を作る。形が違う値・NULL は null（何も出さない）。
export const faviconUrl = path => mediaKey('icon', path) ? `/icon/${path}` : null;

// sites.thumb_path（ハッシュ名 <hash>.webp）から配信 URL を作る。形が違う値・NULL は null（何も出さない）。
export const thumbUrl = path => mediaKey('thumb', path) ? `/thumb/${path}` : null;
