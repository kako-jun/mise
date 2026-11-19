// リンク先のホスト名の表示（表示専用。外部アクセスはしない）。app.js（一覧）・feed.js（縦フィード）・src/seo.js（SSR）が共有する 1 本（countries.js と同じ流儀で globalThis に置く）。
(g => {
const punyDecode = s => { // RFC 3492
  const out = []; let n = 128, i = 0, bias = 72, b = s.lastIndexOf('-'); if (b < 0) b = 0;
  for (let j = 0; j < b; j++) { if (s.charCodeAt(j) >= 128) throw new Error('bad'); out.push(s.charCodeAt(j)); }
  const adapt = (d, num, first) => { d = first ? Math.floor(d / 700) : d >> 1; d += Math.floor(d / num); let k = 0; while (d > 455) { d = Math.floor(d / 35); k += 36; } return k + Math.floor(36 * d / (d + 38)); };
  for (let idx = b > 0 ? b + 1 : 0; idx < s.length;) {
    const oldi = i; let w = 1;
    for (let k = 36; ; k += 36) { if (idx >= s.length) throw new Error('bad'); const c = s.charCodeAt(idx++), dg = c - 48 < 10 ? c - 22 : c - 65 < 26 ? c - 65 : c - 97 < 26 ? c - 97 : 36; if (dg >= 36) throw new Error('bad'); i += dg * w; const t = k <= bias ? 1 : k >= bias + 26 ? 26 : k - bias; if (dg < t) break; w *= 36 - t; }
    bias = adapt(i - oldi, out.length + 1, oldi === 0); n += Math.floor(i / (out.length + 1)); i %= out.length + 1; if (n > 0x10ffff) throw new Error('bad'); out.splice(i++, 0, n);
  }
  return String.fromCodePoint(...out);
};
  // Unicode に戻してよいラベルだけ戻す（偽装ドメイン対策。ブラウザの IDN 表示と同じ安全側）: (a) 漢字・ひらがな・カタカナ・長音・ラテン・数字・ハイフンだけ、または (b) ラテンと紛らわしくない 1 つの文字体系だけ。
  const JP_LATIN = /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\u30fc\p{Script=Latin}0-9-]+$/u;
  const SINGLE = ['Hangul', 'Thai', 'Arabic', 'Hebrew', 'Devanagari', 'Bengali', 'Tamil', 'Telugu', 'Gujarati', 'Kannada', 'Malayalam', 'Khmer', 'Lao', 'Georgian', 'Ethiopic', 'Sinhala', 'Myanmar', 'Tibetan'].map(n => new RegExp(`^[\\p{Script=${n}}0-9-]+$`, 'u'));
  const safeLabel = l => JP_LATIN.test(l) || SINGLE.some(re => re.test(l));
  const siteHost = value => {
    let u; try { u = new URL(value); } catch (_) { return null; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    const h = u.hostname; if (!h) return null;
    if (!/(^|\.)xn--/i.test(h)) return h;
    try {
      const labels = h.split('.').map(l => /^xn--/i.test(l) ? punyDecode(l.slice(4)) : l);
      return labels.every((l, i) => !/^xn--/i.test(h.split('.')[i]) || safeLabel(l)) ? labels.join('.') : h;
    } catch (_) { return h; }
  };
  g.MiseSiteHost = siteHost;
})(globalThis);
