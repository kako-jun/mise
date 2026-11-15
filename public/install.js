// インストール提案バナーの純粋な判定(#105)。DOM に触れないので tests/install-banner.test.mjs が実挙動で検査する。
// beforeinstallprompt の捕捉もここ(app.js より前に読み込まれる)。app.js の起動中に同期例外が出ても標準提案を止められる。表示の配線は app.js 側(言語切替と同じ t() を使うため)。
(function () {
  // [あとで]を押した後、再び出すまでの期間(ミリ秒)。
  const DISMISS_MS = 7 * 24 * 60 * 60 * 1000;
  // 保存値(文字列)から却下時刻を取り出す。未保存・壊れた値は null。
  const parseDismissedAt = raw => {
    if (typeof raw !== 'string' || !/^\d{1,15}$/.test(raw)) return null;
    const n = Number(raw);
    return Number.isSafeInteger(n) && n > 0 ? n : null;
  };
  // 今は抑制中か。却下から DISMISS_MS 以内なら true。未来時刻(時計が大きく進んだ後に戻った場合など)は抑制しない(長期間出なくなるのを避ける)。
  const isDismissActive = (dismissedAt, now, durationMs = DISMISS_MS) => dismissedAt !== null && dismissedAt <= now && now - dismissedAt < durationMs;
  const api = { DISMISS_MS, parseDismissedAt, isDismissActive, event: null };
  window.MiseInstall = api;
  // 標準提案を止めて保持する。app.js は api.event を読み、後から来たときは miseinstallprompt で知らされる。
  window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); api.event = e; window.dispatchEvent(new Event('miseinstallprompt')); });
})();
