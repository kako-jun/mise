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

  // 縦フィードの自動送り(#199, #292)。時間の進みだけの純粋な状態機械(DOM・タイマーに触れない)。自動送りは常に有効(オン/オフは無い)。
  // tick(now, { held, moving, atEnd }) を毎フレーム呼ぶ。now はミリ秒(performance.now / rAF の時刻)。
  // - atEnd(終了カード・0件)のあいだは進めず 0 に戻す。
  // - moving(指が触れている・スクロールや寄せのアニメーション中)のあいだは進行を 0 に捨てて待つ。止まって揃った瞬間に、待ち時間なしで 0 から数え直す(#292。どの経路で切り替わっても同じ)。
  // - held(展開・パネル・検索・hover/focus・タブ非表示)のあいだは進めない(表示は保留時の進行のまま)。held が解けたら resumeDelay 待って 0 から数え直す。
  // - poke(now)(タブ復帰など)でも進行を捨て、resumeDelay 待ってから数え直す。
  // - 1 フレームで進める時間は 100ms まで。前のフレームから gap(既定 1000ms)を超えて空いたとき(タブ非表示・画面ロック・ループ停止の後)は、
  //   held と同じく進行を捨てて resumeDelay 待ってから 0 から数え直す(ブラウザは非表示中 rAF を止めるので held=true の tick が来ない)。
  // 戻り値 { progress: 0..1, advance: 次へ進む合図, state: 'off'|'moving'|'held'|'quiet'|'running' }
  function makeAutoAdvance({ interval, resumeDelay, gap = 1000 }) {
    let elapsed = 0, last = null, quietUntil = 0, wasHeld = false;
    return {
      poke(now) { quietUntil = now + resumeDelay; elapsed = 0; },
      reset() { elapsed = 0; },
      tick(now, { held, moving, atEnd }) {
        const raw = last === null ? 0 : now - last, dt = Math.min(100, Math.max(0, raw)); last = now;
        if (raw > gap) { quietUntil = now + resumeDelay; elapsed = 0; }
        if (atEnd) { elapsed = 0; wasHeld = false; return { progress: 0, advance: false, state: 'off' }; }
        if (moving) { elapsed = 0; return { progress: 0, advance: false, state: 'moving' }; }
        if (held) { wasHeld = true; return { progress: elapsed / interval, advance: false, state: 'held' }; }
        if (wasHeld) { wasHeld = false; quietUntil = now + resumeDelay; elapsed = 0; }
        if (now < quietUntil) return { progress: 0, advance: false, state: 'quiet' };
        elapsed += dt;
        if (elapsed >= interval) { elapsed = 0; return { progress: 1, advance: true, state: 'running' }; }
        return { progress: elapsed / interval, advance: false, state: 'running' };
      },
    };
  }

  // 縦フィードの窓(#278)。現在のカード c の前後だけ中身を描き、離れたカードは空の枠(高さ固定)に戻してノード数を抑える。
  // 描く範囲は [c-behind, c+ahead]、捨てる範囲はそこから margin 離れたところ(境目で描く・捨てるを繰り返さない)。範囲は 0..total-1 に収める。
  function windowRange(c, total, { behind = 2, ahead = 3, margin = 3 } = {}) {
    const lo = Math.max(0, c - behind), hi = Math.min(total - 1, c + ahead);
    return { lo, hi, dropBelow: c - behind - margin, dropAbove: c + ahead + margin };
  }

  // 指を離したときの行き先(#278)。startIdx=触り始めのカード、pos=離した瞬間の scrollTop(px)、h=カードの高さ、v=離す直前の速度(px/ms、下へ進む向きが正)。
  // 速い flick(|v| >= flickV かつ動かした量が minMove 以上)は触り始めから 1 枚だけ。そうでなければ位置で近い方(半分を越えれば次)。どちらも触り始めから ±1 枚までで、2 枚以上は飛ばさない。
  function settleTarget({ startIdx, pos, h, v, count, flickV = 0.3, minMove = 12 }) {
    if (!(h > 0) || !(count > 0)) return 0;
    pos = Math.max(0, Math.min((count - 1) * h, pos)); // 端のゴムバンドで範囲外にいても、先頭〜末尾の中へ丸める
    const moved = pos - startIdx * h; let d;
    if (Math.abs(v) >= flickV && Math.abs(moved) >= minMove) d = v > 0 ? 1 : -1;
    else d = Math.max(-1, Math.min(1, Math.round(pos / h) - startIdx));
    return Math.max(0, Math.min(count - 1, startIdx + d));
  }

  // 触った点列 [{t(ms), y(px)}] から離す直前の速度(px/ms。指が上へ動く=下へ進む向きが正)。終点が離す時刻 end より span ms 以上古ければ止まっていたとして 0
  function releaseVelocity(samples, end, span = 100) {
    if (!samples || samples.length < 2) return 0;
    const last = samples[samples.length - 1]; if (end - last.t > span) return 0;
    let first = last; for (let i = samples.length - 2; i >= 0 && last.t - samples[i].t <= span; i--) first = samples[i];
    const dt = last.t - first.t; return dt > 0 ? (first.y - last.y) / dt : 0;
  }

  // 寄せるアニメーションの時間(ms): 近いほど短く、1 枚分でも 150〜300ms に収める。ease-out(三次)
  const settleMs = (dist, h) => Math.round(150 + 150 * Math.min(1, Math.abs(dist) / (h || 1)));
  const easeOutCubic = x => 1 - Math.pow(1 - Math.max(0, Math.min(1, x)), 3);

  g.MiseFeedLib = { windowRange, settleTarget, releaseVelocity, settleMs, easeOutCubic, makeAutoAdvance, panGeometry, fmtCount, navPosition, isPending, safeHttpUrl, thumbOf, hash32, rngOf, updatedOf, order, inScope, covers, validSeenKey, cleanSeen, parseSeenImport, SEEN_MAX_BYTES };
})(globalThis);
