// 縦フィード試作(#52)の純関数。DOM に触れない。feed.js が使い、tests/feed-view.test.mjs が単体で検証する。
(g => {
  // http(s) だけ許す。javascript: / data: / vbscript: / 相対 / 不正は null（app.js の safeHttpUrl と同じ判定）
  const safeHttpUrl = value => { try { const u = new URL(value); return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null; } catch (_) { return null; } };

  // サムネは同一オリジンの絶対パスだけ。`//host` と `/\host`（ブラウザは \ を / と扱う）は不可。origin を実際に解決して確かめる
  const thumbOf = (v, origin) => {
    if (typeof v !== 'string' || !/^\/(?![\/\\])/.test(v) || /[\u0000-\u001f]/.test(v)) return null;
    try { return new URL(v, origin).origin === origin ? v : null; } catch (_) { return null; }
  };

  function hash32(str) { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function rngOf(seedStr) { let a = hash32(String(seedStr)); return () => { a = (a + 0x6D2B79F5) | 0; let x = Math.imul(a ^ (a >>> 15), 1 | a); x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x; return ((x ^ (x >>> 14)) >>> 0) / 4294967296; }; }

  const updatedOf = s => Date.parse(s.source_updated_at || s.registered_at);
  // 並び。sort='new' は更新が新しい順。それ以外は重み付きランダム（Efraimidis-Spirakis。key = u^(1/w) の降順。
  // w = 1 + boost * 0.5^(経過日/半減期)）。同じ seed・同じ now なら同じ並び
  function order(sites, { sort, seed, now, boost, halfLifeDays }) {
    if (sort === 'new') return [...sites].sort((a, b) => (updatedOf(b) || 0) - (updatedOf(a) || 0) || (a.id < b.id ? -1 : 1));
    const rnd = rngOf(seed);
    return sites.map(s => {
      const days = Math.max(0, (now - (updatedOf(s) || 0)) / 86400000), fresh = Number.isFinite(updatedOf(s)) ? Math.pow(.5, days / halfLifeDays) : 0;
      return { s, key: Math.pow(rnd(), 1 / (1 + boost * fresh)) };
    }).sort((a, b) => b.key - a.key || (a.s.id < b.s.id ? -1 : 1)).map(x => x.s);
  }

  // 範囲: all / 国(jp) / 区画(jp-13)
  const inScope = (s, scope) => scope === 'all' || (scope.includes('-') ? (s.region_code || '').toLowerCase() === scope : (s.country_code || '').toLowerCase() === scope);
  // 読み込み済みの範囲 loaded（全件読めた＝complete のとき）が、要求した範囲 want を含むか（含めば取り直さず絞り込むだけ）
  const covers = (loaded, complete, want) => loaded === want || (complete && (loaded === 'all' || want.startsWith(loaded + '-')));

  // 「見た」の保存・読み込み
  const SEEN_MAX_BYTES = 1000000, SEEN_MAX_ENTRIES = 20000;
  const validSeenKey = k => typeof k === 'string' && /^[\w.-]{1,128}$/.test(k);
  const validSeenTime = v => typeof v === 'string' && !Number.isNaN(Date.parse(v));
  const cleanSeen = sites => Object.fromEntries(Object.entries(sites || {}).filter(([k, v]) => validSeenKey(k) && validSeenTime(v)).slice(0, SEEN_MAX_ENTRIES));
  // JSON 文字列を読み込み用に検証。大きすぎる・形が違う場合は null。返すのは検証済みの {id: 日時}
  function parseSeenImport(text) {
    if (typeof text !== 'string' || text.length > SEEN_MAX_BYTES) return null;
    try { const d = JSON.parse(text); if (!d || d.v !== 1 || !d.sites || typeof d.sites !== 'object' || Array.isArray(d.sites)) return null; return cleanSeen(d.sites); } catch (_) { return null; }
  }

  // 「見た」の滞在タイマー。カードが画面に出てから delay 後に onSeen。一覧を作り直すとき clearAll() で古いタイマーを全部止める
  function makeDwell(delay, onSeen, timers = { setTimeout: (f, ms) => g.setTimeout(f, ms), clearTimeout: t => g.clearTimeout(t) }) {
    const map = new Map();
    return {
      start(key, arg) { this.stop(key); map.set(key, timers.setTimeout(() => { map.delete(key); onSeen(arg); }, delay)); },
      stop(key) { if (map.has(key)) { timers.clearTimeout(map.get(key)); map.delete(key); } },
      clearAll() { map.forEach(t => timers.clearTimeout(t)); map.clear(); },
      size: () => map.size,
    };
  }

  g.MiseFeedLib = { safeHttpUrl, thumbOf, hash32, rngOf, updatedOf, order, inScope, covers, validSeenKey, cleanSeen, parseSeenImport, makeDwell, SEEN_MAX_BYTES };
})(globalThis);
