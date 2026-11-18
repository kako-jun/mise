// /about/ の言語・テーマ切替（#118）。初期値は <head> のインラインスクリプトが決める。保存キーは本体と共通。
(function () {
  const root = document.documentElement, $ = s => document.querySelector(s);
  const save = (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} };
  const words = {
    ja: { title: 'MISE について — 今だからこそ、ディレクトリ型検索エンジン。', theme: 'ダーク/ライト', lang: 'English', home: 'MISE のトップ' },
    en: { title: 'About MISE — A directory search engine for today.', theme: 'Dark / light', lang: '日本語', home: 'MISE Home' },
  };
  function apply() {
    const w = words[root.lang === 'ja' ? 'ja' : 'en'];
    document.title = w.title;
    for (const [sel, text] of [['#theme-toggle', w.theme], ['#language-toggle', w.lang], ['.about-logo', w.home]]) { const e = $(sel); e.setAttribute('aria-label', text); if (e.hasAttribute('title')) e.title = text; }
  }
  $('#language-toggle').addEventListener('click', () => { root.lang = root.lang === 'ja' ? 'en' : 'ja'; save('mise-language', root.lang); apply(); });
  $('#theme-toggle').addEventListener('click', () => { const t = root.dataset.theme === 'dark' ? 'light' : 'dark'; root.dataset.theme = t; save('mise-theme', t); });
  apply();
})();
