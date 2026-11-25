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

  // 件数の桁区切り（表示言語に合わせる）。数でない値は NaN と出さず空文字
  const fmtCount = (n, lang) => Number.isFinite(Number(n)) && n !== null && n !== '' ? Number(n).toLocaleString(lang === 'ja' ? 'ja-JP' : 'en-US') : '';
  // 縦フィードの現在位置(#204)。current は 0 始まりのカード番号(終了カードは list 件数と同じ値)、loaded は読み込み済み件数、more は続きがある/読み込みに失敗した(=分母が確定しない)か。
  // 0 件は null(出さない)。分子は 1〜loaded に丸め、more のときは分母に + を付けて「3 / 500+」とする。
  const navPosition = (current, loaded, more, lang) => {
    const n = Math.floor(Number(loaded));
    if (!Number.isFinite(n) || n < 1) return null;
    const cur = Math.max(1, Math.min(n, Math.floor(Number(current) || 0) + 1));
    return { cur, total: n, more: !!more, denom: `${fmtCount(n, lang)}${more ? '+' : ''}`, text: `${fmtCount(cur, lang)} / ${fmtCount(n, lang)}${more ? '+' : ''}` };
  };
  // 要求した範囲 want のデータがまだ揃っていない（絞り込み・描画を保留する）か
  const isPending = (loaded, complete, want) => !covers(loaded, complete, want);

  // サムネのパン(#173)。枠(fw×fh)に object-fit: cover で置く縦横比 nw:nh の画像が、横に何 px 見切れるか。
  // 見切れ(画像の実幅 - 枠幅)が 8px 以上のときだけ { w: 画像の実幅, px: 片側の移動量 } を返し、それ未満・画像が枠より狭い・0 や NaN は null(動かさない)。
  // 画像は scale(1.04) で中央から広がるが、広がる分は余白になるだけなので、移動量 px の範囲では端が見えない(計算に含めない)。
  const PAN_MIN_OVER = 8;
  const panGeometry = (fw, fh, nw, nh) => {
    if (![fw, fh, nw, nh].every(v => typeof v === 'number' && Number.isFinite(v) && v > 0)) return null;
    const w = fh * nw / nh, over = w - fw;
    return over >= PAN_MIN_OVER ? { w, px: over / 2 } : null;
  };

  // 縦フィードの自動送り(#199)。時間の進み・停止・再開だけの純粋な状態機械(DOM・タイマーに触れない)。
  // tick(now, { enabled, held, atEnd }) を毎フレーム呼ぶ。now はミリ秒(performance.now / rAF の時刻)。
  // - enabled=false か atEnd(終了カード・0件)のあいだは進めず 0 に戻す。
  // - held(展開・パネル・検索・hover/focus・タブ非表示)のあいだは進めない(表示は保留時の進行のまま)。held が解けたら resumeDelay 待って 0 から数え直す。
  // - poke(now)(手動操作)でも進行を捨て、resumeDelay 待ってから数え直す。
  // - 1 フレームで進める時間は 100ms まで。前のフレームから gap(既定 1000ms)を超えて空いたとき(タブ非表示・画面ロック・ループ停止の後)は、
  //   held と同じく進行を捨てて resumeDelay 待ってから 0 から数え直す(ブラウザは非表示中 rAF を止めるので held=true の tick が来ない)。
  // 戻り値 { progress: 0..1, advance: 次へ進む合図, state: 'off'|'held'|'quiet'|'running' }
  function makeAutoAdvance({ interval, resumeDelay, gap = 1000 }) {
    let elapsed = 0, last = null, quietUntil = 0, wasHeld = false;
    return {
      poke(now) { quietUntil = now + resumeDelay; elapsed = 0; },
      reset() { elapsed = 0; },
      tick(now, { enabled, held, atEnd }) {
        const raw = last === null ? 0 : now - last, dt = Math.min(100, Math.max(0, raw)); last = now;
        if (raw > gap) { quietUntil = now + resumeDelay; elapsed = 0; }
        if (!enabled || atEnd) { elapsed = 0; wasHeld = false; return { progress: 0, advance: false, state: 'off' }; }
        if (held) { wasHeld = true; return { progress: elapsed / interval, advance: false, state: 'held' }; }
        if (wasHeld) { wasHeld = false; quietUntil = now + resumeDelay; elapsed = 0; }
        if (now < quietUntil) return { progress: 0, advance: false, state: 'quiet' };
        elapsed += dt;
        if (elapsed >= interval) { elapsed = 0; return { progress: 1, advance: true, state: 'running' }; }
        return { progress: elapsed / interval, advance: false, state: 'running' };
      },
    };
  }

  g.MiseFeedLib = { makeAutoAdvance, panGeometry, fmtCount, navPosition, isPending, safeHttpUrl, thumbOf, hash32, rngOf, updatedOf, order, inScope, covers, validSeenKey, cleanSeen, parseSeenImport, SEEN_MAX_BYTES };
})(globalThis);
