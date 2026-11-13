// セキュリティヘッダ。CSP は強制(enforce)。Report-Only で試すときは CSP_REPORT_ONLY を true にする。
export const CSP_REPORT_ONLY = false;
const CSP_HEADER = CSP_REPORT_ONLY ? 'Content-Security-Policy-Report-Only' : 'Content-Security-Policy';

// HTML 以外の応答用。何も読み込まない。
const STATIC_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'";

// 画面用。script は同一オリジン＋インラインスクリプトの sha256 だけ許す(unsafe-inline なし)。
// style は同一オリジンのみ(インライン style 属性・タグは使わない。JS からの CSSOM 操作は対象外)。
export function pageCsp(scriptHashes) {
  const script = ["'self'", ...scriptHashes.map(h => `'sha256-${h}'`)].join(' ');
  return [
    "default-src 'none'", `script-src ${script}`, "style-src 'self'", "img-src 'self' data:",
    "connect-src 'self'", "font-src 'self'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'", "object-src 'none'",
  ].join('; ');
}

export function securityHeaders(csp = STATIC_CSP) {
  return {
    [CSP_HEADER]: csp,
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
  };
}

// HTML 中の実行されるインライン <script>（src なし・JSON-LD 等のデータブロック以外）の sha256(base64) 一覧
export async function inlineScriptHashes(html) {
  const hashes = [];
  for (const m of html.matchAll(/<script(\s[^>]*)?>([\s\S]*?)<\/script>/gi)) {
    const attrs = m[1] || '';
    if (/\ssrc=/i.test(attrs) || /type\s*=\s*["']application\/(ld\+)?json["']/i.test(attrs) || !m[2].trim()) continue;
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(m[2]));
    hashes.push(btoa(String.fromCharCode(...new Uint8Array(digest))));
  }
  return hashes;
}
