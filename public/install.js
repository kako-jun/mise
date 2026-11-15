// インストール提案バナーの純粋な判定(#105)。DOM に触れないので tests/install-banner.test.mjs が実挙動で検査する。
// 表示・イベントの配線は app.js 側(言語切替と同じ t() を使うため)。
(function () {
  // [あとで]を押した後、再び出すまでの期間(ミリ秒)。
  const DISMISS_MS = 7 * 24 * 60 * 60 * 1000;
  // 保存値(文字列)から却下時刻を取り出す。未保存・壊れた値は null。
  const parseDismissedAt = raw => {
    if (typeof raw !== 'string' || !/^\d{1,15}$/.test(raw)) return null;
    const n = Number(raw);
    return Number.isSafeInteger(n) && n > 0 ? n : null;
  };
  // 今は抑制中か。却下から DISMISS_MS 以内なら true。未来時刻(時計の巻き戻し)も保守的に抑制中とみなす。
  const isDismissActive = (dismissedAt, now, durationMs = DISMISS_MS) => dismissedAt !== null && now - dismissedAt < durationMs;
  window.MiseInstall = { DISMISS_MS, parseDismissedAt, isDismissActive };
})();
