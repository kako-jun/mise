// 新着とカテゴリ別の縦フィード。 `?view=feed` のときだけ index.html が読み込む。
(() => {
  // ---- 調整できる定数（触って決める） -------------------------------------------------
  const FRESH_BOOST = 1.5;       // 更新が新しいサイトの出やすさの上乗せ。0=完全ランダム、大きいほど新しい順に近づく
  const FRESH_HALF_LIFE_DAYS = 60; // 更新から何日で優遇が半分になるか
  const GLOBE_DEBOUNCE_MS = 280;   // 速くスクロールしている間は地球を動かさない
  const IN_VIEW_THRESHOLD = 0.6;   // カードがどれだけ見えたら「いまのカード」とみなすか(「見た」の判定にも使う)
  const SEEN_DWELL_MS = 1500;      // 「見た」= 上の割合以上が画面に出たまま、これだけ経過(素通りは数えない)
  const MAX_SITES = 500;         // 1 回の範囲選択で読み込むサイト数の上限(100 件ずつ取得 = 最大 5 リクエスト)
  const MILESTONES = [10, 50, 100, 300, 1000]; // 節目
  const SEEN_KEY = 'mise-seen-v1'; // 端末内保存(localStorage)。{v:1, sites:{サイトID: 初めて見た日時ISO}}
  // -------------------------------------------------------------------------------------

  const L = window.MiseFeedLib;
  const $ = (s, r = document) => r.querySelector(s);
  const storage = { get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
    sget(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }, sset(k, v) { try { sessionStorage.setItem(k, v); } catch (_) {} } };
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const words = {
    ja: { searchLabel: 'サイトを検索', clearSearch: '検索語を消去', moreInList: '続きは箇条書きで見る', seen: '見たサイト', seenTitle: n => `見たサイト: ${n}件(タップで詳細)`, seenClose: '閉じる', untilNext: n => `次の節目まであと ${n}件`, topMilestone: 'すべての節目に到達', byMonth: '月ごと', byCountry: '国別', byCat: 'カテゴリ別', exportJson: 'JSONを書き出す', importJson: 'JSONを読み込む', importOk: n => `${n}件を取り込みました`, importBad: '読み込めませんでした', seenNote: 'この端末だけに保存されています。', noCountry: '国不明',
      directoryTitle: '箇条書きに戻る', noteFail: '一部の読み込みに失敗しました。', open: 'サイトを開く', openNew: '新しいタブで開く', more: 'もっと見る', less: '閉じる', next: '次へ', prev: '前へ', updated: '更新', added: '登録', end: n => `ここまでです（${n}件）。`, top: '最初に戻る', reshuffle: '並びを変えてもう一度', empty: '条件に合うサイトはありません。', error: 'データを取得できませんでした。再読み込みしてください。', loading: '読み込み中', feedLabel: 'サイトのフィード', hint: 'j / k または ↑ ↓ で移動', fallback: '' },
    en: { searchLabel: 'Search sites', clearSearch: 'Clear search', moreInList: 'See more in list view', seen: 'Sites seen', seenTitle: n => `Sites seen: ${n} (tap for details)`, seenClose: 'Close', untilNext: n => `${n} to the next milestone`, topMilestone: 'All milestones reached', byMonth: 'By month', byCountry: 'By country', byCat: 'By category', exportJson: 'Export JSON', importJson: 'Import JSON', importOk: n => `Imported ${n}`, importBad: 'Could not read that file', seenNote: 'Stored on this device only.', noCountry: 'Unknown',
      directoryTitle: 'Back to list view', noteFail: 'Some sites could not be loaded.', open: 'Open site', openNew: 'Open in new tab', more: 'Read more', less: 'Close', next: 'Next', prev: 'Previous', updated: 'Updated', added: 'Added', end: n => `End of this feed (${n} sites).`, top: 'Back to the first', reshuffle: 'Reshuffle and go again', empty: 'No sites match.', error: 'Could not load data. Please reload.', loading: 'Loading', feedLabel: 'Site feed', hint: 'j / k or ↑ ↓ to move', fallback: ' (Japanese)' }
  };
  // 通常ページ(app.js)と同じ決め方: 保存値、無ければブラウザの言語
  const savedLang = storage.get('mise-language');
  const language = savedLang === 'ja' || savedLang === 'en' ? savedLang : (((navigator.languages && navigator.languages[0]) || navigator.language || '').toLowerCase().indexOf('ja') === 0 ? 'ja' : 'en');
  const newTab = storage.get('mise-external-new-tab') === 'true';
  const t = k => words[language][k];
  // サイト名: 画面の言語の名前があればそれ、無ければ言語共通の name(#167)
  const siteName = s => (language === 'ja' ? s.name_ja : s.name_en) || s.name;
  // 件数の桁区切り(表示言語に合わせる)。「SEEN 000128」のような固定桁の意匠には使わない
  const fmt = n => L.fmtCount(n, language);

  // ---- 決定論の乱数（シード = セッションごと。?seed= で再現） ----
  const { hash32 } = L;

  const params = new URLSearchParams(location.search);
  let seed = params.get('seed') || storage.sget('mise-feed-seed') || Math.random().toString(36).slice(2, 8);
  storage.sset('mise-feed-seed', seed);
  const requestedPath = params.getAll('category');
  const filterQuery = params.get('q') || '';
  const filterTags = params.getAll('tag');
  const filtered = !!filterQuery.trim() || filterTags.length > 0;
  const sort = params.get('sort') === 'new' ? 'new' : 'mix'; // 旧 ?cat= リンクの並び
  let scope, startPage, path = [], categories = [], all = [], list = [], current = 0, cards = [], loadFailed = false, hasMore = false, loadedCount = 0;

  // 「見た」の滞在タイマー。一覧を作り直すとき clearAll() で古いタイマーを止める(止めないと見ていないサイトを数える)
  const dwell = L.makeDwell(SEEN_DWELL_MS, site => { if (!document.hidden) markSeen(site); });
  let root, scroller, bar, observer, globeTimer = 0, lastPlace = '', seenTrigger;

  // ---- データ整形 ----
  function rootCat(id) { let n = categories.find(c => c.id === id); while (n && n.parent_id) n = categories.find(c => c.id === n.parent_id); return n || null; }
  // /api/sites は 1 サイト 1 行（category_ids を持つ）。同じ id が複数ページに跨っても 1 件にまとめる
  function build(rows) {
    const map = new Map();
    for (const r of rows) {
      if (map.has(r.id)) continue;
      const roots = new Set();
      for (const id of Array.isArray(r.category_ids) ? r.category_ids : []) { const rc = rootCat(id); if (rc) roots.add(rc.id); }
      map.set(r.id, { ...r, roots });
    }
    return [...map.values()];
  }
  function resolveCategoryPath() {
    const parts = requestedPath.length ? requestedPath : (params.get('cat') ? [params.get('cat')] : []);
    let parent = null;
    const resolved = [];
    for (const part of parts) {
      const folder = categories.find(c => c.parent_id === parent && (c.id === part || c.name_ja === part));
      if (!folder) return [];
      parent = folder.id;
      resolved.push(parent);
    }
    return resolved;
  }
  function visible() {
    if (requestedPath.length || filtered) return all; // 絞り込み時は箇条書きと同じ順序
    return L.order(all, { sort, seed, now: Date.now(), boost: FRESH_BOOST, halfLifeDays: FRESH_HALF_LIFE_DAYS }).filter(s => !path.length || s.roots.has(path[0]));
  }

  // 第三者へアクセスしないよう、同一オリジンの絶対パスだけ許す(//host と /\host は不可。feed-lib.js)
  const thumbOf = s => L.thumbOf(s.thumb_url, location.origin);

  // ---- 場所（国旗 + 区画名） ----
  function regionInfo(s) {
    const c = s.region_code;
    return window.MISE_REGIONS && /^[A-Z]{2}-[A-Z0-9]{1,3}$/.test(c || '') && c.slice(0, 2) === s.country_code ? window.MISE_REGIONS[c] || null : null;
  }
  function place(s) {
    const info = window.MISE_COUNTRIES && /^[A-Z]{2}$/.test(s.country_code || '') ? window.MISE_COUNTRIES[s.country_code] : null;
    if (!info) return null;
    const region = regionInfo(s), wrap = document.createElement('p'); wrap.className = 'f-place';
    if ((window.MISE_FLAGS || []).includes(s.country_code)) {
      const img = document.createElement('img'); img.alt = ''; img.width = 18; img.height = 14; img.src = `/flags/${s.country_code.toLowerCase()}.svg`; img.loading = 'lazy'; img.decoding = 'async';
      img.addEventListener('error', () => img.remove()); wrap.append(img);
    }
    const label = document.createElement('span'); label.textContent = region ? `${info[language]} · ${region[language]}` : info[language]; wrap.append(label);
    return wrap;
  }

  // ---- カード ----
  function configureLink(a) { if (newTab) { a.target = '_blank'; a.rel = 'noopener noreferrer'; } else { a.removeAttribute('target'); a.removeAttribute('rel'); } }
  // ---- モック専用のダミー長文（200〜400字）。本番データには入れない。long_description_ja/en があればそちらを使う ----
  const MOCK_LONG = {
    ja: [
      'これはモック用のダミー文です。ここに入るのは、サイトの作者自身が書いた長めの紹介を想定した文章です。何のために作ったのか、どんな人に見てほしいのか、更新はどのくらいの頻度か、といった背景を、検索結果の一行説明では書ききれない分だけゆっくり書きます。読みやすさを確かめるため、あえて少し長くしてあります。行間、文字の大きさ、背景のぼかしと暗さが、写真の明るい部分に重なっても読めるかを見てください。タップすると全文が開き、もう一度タップすると閉じます。長いときはこの枠の中だけが縦にスクロールします。',
      'モック用のダミー文その二です。個人で十年以上続けている小さなサイトの紹介を想定しています。はじめは自分のためのメモ帳でしたが、読んでくれる人が少しずつ増え、いまは季節ごとの記録と、道具や手順の覚え書きが中心になりました。派手な仕掛けはありませんが、古い記事も直しながら残しています。本番のデータではありません。表示の確認のためだけに、同じ話題を言い換えながら字数を稼いでいます。折りたたみ時は数行だけ見え、続きは「もっと見る」から読めます。',
      'ダミーの長文、三つ目。これは短めのパターンで、二百字ほどです。作者の一言と、サイトの見どころを数行にまとめた、という体の文章を入れておきます。見どころは三つ。ひとつめは手書きの図、ふたつめは年表、みっつめは読者からの訂正をそのまま載せる欄です。モック用であり、実在のサイトの紹介ではありません。'
    ],
    en: [
      'This is placeholder text for the mockup. In the real data this slot would hold a longer introduction written by the site’s own author: why it was made, who it is for, and how often it changes — the things a one-line description has no room for. It is deliberately a little long so you can judge readability: line height, type size, and whether the blur and the dark scrim keep the words legible over the bright parts of a picture. Tap to open the full text, tap again to fold it. When it runs long, only this box scrolls vertically.',
      'Placeholder number two, imagined as the introduction to a small personal site kept up for more than ten years. It began as a private notebook; readers trickled in, and today it is mostly seasonal records and notes on tools and procedures. There are no flashy tricks, and old posts are fixed and kept rather than deleted. This is not real data. It exists only to check the layout, restating the same idea to reach a useful length. Collapsed, you see a few lines; the rest opens with “Read more”.',
      'Placeholder number three, the short pattern at around two hundred characters: a few lines pairing the author’s own words with the highlights of the site. Three highlights: hand-drawn diagrams, a timeline, and a column that prints reader corrections as they arrive. This is mockup text and does not describe any real site.'
    ]
  };
  // 長文が無いサイトは長文欄ごと出さない。ダミーは開発用の ?mock=1 のときだけ
  const useMock = params.get('mock') === '1';
  function longText(s) {
    const own = language === 'en' ? s.long_description_en : s.long_description_ja;
    if (own) return { text: own, mock: false };
    if (!useMock) return null;
    const pool = MOCK_LONG[language];
    return { text: pool[hash32('long:' + s.id) % pool.length], mock: true };
  }
  // 表示中のカードのサムネだけ、見切れ幅(L.panGeometry)を測って CSS 変数で渡す。複数枚でも、読みを先にまとめてから書く(強制レイアウトを繰り返さない)
  function panFit(heroes) {
    const reads = [];
    for (const hero of heroes) { const img = hero.querySelector('img.f-img'); reads.push([hero, img && L.panGeometry(hero.clientWidth, hero.clientHeight, img.naturalWidth, img.naturalHeight)]); }
    for (const [hero, g] of reads) {
      for (const el of hero.querySelectorAll('img')) { if (g) el.dataset.pan = '1'; else delete el.dataset.pan; }
      if (g) { hero.style.setProperty('--pan-w', g.w + 'px'); hero.style.setProperty('--pan-px', g.px + 'px'); } else { hero.style.removeProperty('--pan-w'); hero.style.removeProperty('--pan-px'); }
    }
  }
  // 寸法が変わったら、離れて paused になったカードの古い translate・--pan-w/--pan-px を捨てる(回転後に戻ると古い幅で横に帯が出る)。読まずに書くだけ。次に表示になったとき panFit が測り直し、負の delay で中央から始まる
  function clearIdlePan() { for (const hero of document.querySelectorAll('#feed .f-card:not(.is-active) .f-hero')) { for (const el of hero.querySelectorAll('img[data-pan]')) delete el.dataset.pan; hero.style.removeProperty('--pan-w'); hero.style.removeProperty('--pan-px'); } }
  const activeHeroes = () => [...document.querySelectorAll('#feed .f-card.is-active .f-hero')];
  function card(s, i, total) {
    const art = document.createElement('article'); art.className = 'f-card'; art.tabIndex = -1; art.dataset.index = i; art.dataset.id = s.id;
    art.setAttribute('aria-posinset', i + 1); art.setAttribute('aria-setsize', total); art.setAttribute('aria-labelledby', `f-name-${i}`);
    // 横長の1枚(幅320px前後想定)を上部に幅いっぱいで置き、同じ絵を強く拡大・ぼかしてカード全体の背景に敷く。
    // thumb_lqip(極小base64)を先にぼかして出し、画像が届いたらなめらかに入れ替える
    const pic = document.createElement('div'); pic.className = 'f-art';
    const bg = document.createElement('div'); bg.className = 'f-bg'; const hero = document.createElement('div'); hero.className = 'f-hero'; pic.append(bg, hero);
    const thumb = thumbOf(s), lqip = typeof s.thumb_lqip === 'string' && /^data:image\/(webp|jpeg|png);base64,/.test(s.thumb_lqip) ? s.thumb_lqip : null;
    for (const layer of [bg, hero]) {
      if (thumb) {
        if (lqip) { const l = document.createElement('img'); l.className = 'f-lqip'; l.alt = ''; l.src = lqip; layer.append(l); }
        const img = document.createElement('img'); img.className = 'f-img'; img.alt = ''; img.decoding = 'async'; img.loading = i < 2 ? 'eager' : 'lazy';
        img.addEventListener('load', () => { img.classList.add('is-in'); if (layer === hero && art.classList.contains('is-active')) panFit([hero]); }); img.addEventListener('error', () => { layer.replaceChildren(); if (layer === hero) { hero.style.removeProperty('--pan-w'); hero.style.removeProperty('--pan-px'); } }); img.src = thumb; layer.append(img);
      }
    }
    const body = document.createElement('div'); body.className = 'f-body';
    const h = document.createElement('h2'); h.id = `f-name-${i}`; h.className = 'f-name'; h.textContent = siteName(s);
    const useEn = language === 'en' && s.description_en, p = document.createElement('p'); p.className = 'f-desc'; p.textContent = useEn ? s.description_en : s.description_ja + t('fallback'); if (language === 'en' && !useEn) p.lang = 'ja';
    body.append(h);
    const host = globalThis.MiseSiteHost && globalThis.MiseSiteHost(s.url); if (host) { const hs = document.createElement('p'); hs.className = 'f-host'; hs.textContent = host; body.append(hs); }
    body.append(p);
    const pl = place(s); if (pl) body.append(pl);
    // 長い紹介文（ショート動画のキャプション風）。折りたたみ時は数行、タップで全文。長ければ枠内だけ縦スクロール
    const lt = longText(s);
    if (lt) {
      const cap = document.createElement('div'); cap.className = 'f-cap'; cap.dataset.mock = lt.mock ? 'true' : 'false';
      const lp = document.createElement('p'); lp.className = 'f-long'; lp.id = `f-long-${i}`; lp.textContent = lt.text;
      if (language === 'en' && !s.long_description_en && !lt.mock) lp.lang = 'ja';
      const more = document.createElement('button'); more.type = 'button'; more.className = 'f-more'; more.setAttribute('aria-expanded', 'false'); more.setAttribute('aria-controls', lp.id); more.textContent = t('more');
      const toggle = () => { const on = cap.classList.toggle('is-open'); more.setAttribute('aria-expanded', String(on)); more.textContent = t(on ? 'less' : 'more'); lp.tabIndex = on ? 0 : -1; if (!on) lp.scrollTop = 0; };
      more.addEventListener('click', toggle);
      lp.addEventListener('click', () => { if (String(getSelection()).length) return; toggle(); });
      cap.append(lp, more); body.append(cap);
    }
    let tags = []; try { tags = JSON.parse(s.tags_json || '[]'); } catch (_) {}
    if (tags.length) { const ul = document.createElement('ul'); ul.className = 'f-tags'; for (const tg of tags) { const li = document.createElement('li'); li.textContent = (language === 'en' && tg.en) ? tg.en : tg.ja; ul.append(li); } body.append(ul); }
    const foot = document.createElement('div'); foot.className = 'f-foot';
    const sourceDate = path[0] !== globalThis.MiseFeedEntry.RECENT_CATEGORY && s.source_updated_at;
    const when = (sourceDate || s.registered_at || '').slice(0, 10);
    if (when) { const time = document.createElement('time'); time.className = 'f-date'; time.dateTime = when; time.textContent = `${sourceDate ? t('updated') : t('added')} ${when}`; foot.append(time); }
    // http(s) 以外(javascript: / data: / 相対 / 不正)の URL は『開く』リンクを出さない
    const href = L.safeHttpUrl(s.url);
    if (href) {
      const actions = document.createElement('div'); actions.className = 'f-open-actions';
      const a = document.createElement('a'); a.className = 'f-open'; a.href = href;
      const icon = document.createElement('span'); icon.className = 'f-open-icon'; icon.setAttribute('aria-hidden', 'true'); icon.textContent = '→';
      const text = document.createElement('span'); text.textContent = t('open'); a.append(icon, text);
      a.setAttribute('aria-label', `${t('open')}: ${siteName(s)}`); configureLink(a);
      const external = document.createElement('a'); external.className = 'f-open-new'; external.href = href;
      external.target = '_blank'; external.rel = 'noopener noreferrer';
      const label = `${t('openNew')}: ${siteName(s)}`; external.setAttribute('aria-label', label); external.title = label;
      const arrow = document.createElement('span'); arrow.setAttribute('aria-hidden', 'true'); arrow.textContent = '↗'; external.append(arrow);
      actions.append(a, external); foot.append(actions);
    }
    body.append(foot);
    const scrim = document.createElement('div'); scrim.className = 'f-scrim'; scrim.setAttribute('aria-hidden', 'true');
    art.append(pic, scrim, body);
    return art;
  }
  function endCard(n) {
    const sec = document.createElement('section'); sec.className = 'f-card f-end'; sec.tabIndex = -1; sec.dataset.index = 'end'; sec.setAttribute('aria-label', t('end')(fmt(n)));
    const body = document.createElement('div'); body.className = 'f-end-body';
    const h = document.createElement('h2'); h.textContent = t('end')(fmt(n));
    const row = document.createElement('div'); row.className = 'f-end-actions';
    const top = document.createElement('button'); top.type = 'button'; top.textContent = t('top'); top.addEventListener('click', () => { navTarget = 0; clearTimeout(navTimer); navTimer = setTimeout(() => { navTarget = null; }, 900); const c = go(0); if (c) c.focus({ preventScroll: true }); });
    row.append(top);
    if (!requestedPath.length && sort === 'mix') { const re = document.createElement('button'); re.type = 'button'; re.className = 'f-quiet'; re.textContent = t('reshuffle'); re.addEventListener('click', () => { seed = Math.random().toString(36).slice(2, 8); storage.sset('mise-feed-seed', seed); const url = new URL(location.href); url.searchParams.set('seed', seed); history.replaceState(null, '', url); renderList(); const c = go(0, true); if (c) c.focus({ preventScroll: true }); }); row.append(re); }
    body.append(h);
    if (loadFailed) { const note = mk('p'); note.textContent = t('noteFail'); body.append(note); }
    if (hasMore || loadFailed) {
      const href = requestedPath.length && !filtered
        ? globalThis.MiseFeedEntry.continueListHref(scope, path, startPage, loadedCount)
        : filtered ? filteredContinueHref() : directoryHref();
      const more = mk('a', 'f-continue', { href }); more.textContent = t('moreInList'); row.append(more);
    }
    row.append(seenButton());
    body.append(row); sec.append(body);
    return sec;
  }
  function emptyCard() {
    const sec = document.createElement('section'); sec.className = 'f-card f-end'; const b = document.createElement('div'); b.className = 'f-end-body'; const h = document.createElement('h2'); h.textContent = t('empty'); b.append(h); sec.append(b); return sec;
  }


  // ---- 見たサイトの累計(#58)。端末内のみ。重複なし・単調増加・ストリークなし ----
  let seen = { v: 1, sites: {} };
  function loadSeen() {
    try { const d = JSON.parse(storage.get(SEEN_KEY) || 'null'); if (d && d.v === 1 && d.sites && typeof d.sites === 'object') seen = { v: 1, sites: L.cleanSeen(d.sites) }; } catch (_) {}
  }
  const seenCount = () => Object.keys(seen.sites).length;
  const nextMilestone = n => MILESTONES.find(m => m > n) || null;
  function markSeen(site) {
    if (!site || seen.sites[site.id]) return;
    seen.sites[site.id] = new Date().toISOString(); storage.set(SEEN_KEY, JSON.stringify(seen));
    const n = seenCount(); updateSeenBadge(); surfHop();
    if (!reduced.matches) { const num = $('.f-seen-num'); num && num.animate([{ transform: 'scale(1)' }, { transform: 'scale(1.35)' }, { transform: 'scale(1)' }], { duration: 380, easing: 'ease-out' }); }
    if (MILESTONES.includes(n)) { const kit = $('.f-kittens'); if (kit) { kit.innerHTML = kittensHtml(); const last = kit.lastElementChild;
      if (last && !reduced.matches) { const a = last.animate([{ opacity: 0, transform: 'translateY(-14px) rotate(-20deg)' }, { opacity: 1, transform: 'translateY(2px)' }, { transform: 'translateY(0)' }], { duration: 700, easing: 'ease-out' }); addEventListener('pointerdown', () => a.finish(), { once: true }); } } }
    if ($('.f-panel') && !$('.f-panel').hidden) renderPanel();
  }
  const kittenSvg = i => `<svg class="f-kitten" viewBox="0 0 16 16" width="14" height="14"><path fill="${i % 2 ? '#252c30' : '#df7f3e'}" d="M2 1.5 6 4 3 6.5z"/><path fill="${i % 2 ? '#df7f3e' : '#252c30'}" d="M14 1.5 10 4l3 2.5z"/><path d="M3 5.5h10v6.5c0 1.5-1.6 2.5-5 2.5s-5-1-5-2.5z" fill="#fff3df"/><path d="M3 5.5h4.5l-1 4H3z" fill="${i % 2 ? '#252c30' : '#df7f3e'}"/><circle cx="6" cy="9.5" r=".9" fill="#17252b"/><circle cx="10" cy="9.5" r=".9" fill="#17252b"/></svg>`;
  // 節目に届いた数だけ子猫が並ぶ(10件で1匹、50件で2匹…)。保存データからの導出なので別管理しない
  const kittensHtml = () => MILESTONES.filter(m => m <= seenCount()).map((m, i) => kittenSvg(i)).join('');
  function updateSeenBadge() {
    const surf = $('.f-surf'); if (surf) { const sn = seenCount(); $('.f-surf-num', surf).textContent = String(sn).padStart(6, '0'); surf.title = surf.ariaLabel = t('seenTitle')(fmt(sn)); }
    const btn = $('.f-seen'); if (!btn) return; const n = seenCount(), m = nextMilestone(n);
    $('.f-seen-num', btn).textContent = String(n).padStart(6, '0'); btn.title = btn.ariaLabel = t('seenTitle')(fmt(n));
    const bar = $('.f-seen-bar i', btn); if (bar) { const prev = [0, ...MILESTONES].filter(x => x <= n).pop(); bar.style.width = m ? `${Math.round((n - prev) / (m - prev) * 100)}%` : '100%'; }
  }
  function monthKey(iso) { const d = new Date(iso); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; }
  function breakdown(list, label) {
    const ul = mk('ul', 'f-bd'); const max = Math.max(1, ...list.map(x => x[1]));
    for (const [k, v] of list) { const li = mk('li'); const name = mk('span', 'f-bd-k'); name.textContent = label(k); const track = mk('span', 'f-bd-t', { 'aria-hidden': 'true' }); const i = mk('i'); i.style.width = `${v / max * 100}%`; track.append(i); const num = mk('span', 'f-bd-v'); num.textContent = fmt(v); li.append(name, track, num); ul.append(li); }
    return ul;
  }
  function renderPanel() {
    const panel = $('.f-panel'); const n = seenCount(), m = nextMilestone(n); panel.replaceChildren();
    const head = mk('div', 'f-panel-head'); const h = mk('h2'); h.textContent = `${t('seen')}: ${fmt(n)}`; const x = mk('button', 'f-panel-x', { type: 'button', 'aria-label': t('seenClose') }); x.innerHTML = '<svg aria-hidden="true" viewBox="0 0 8 8"><path d="M1 1l6 6M7 1l-6 6" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>'; x.addEventListener('click', closePanel); head.append(h, x);
    const note = mk('p', 'f-panel-note'); note.textContent = m ? `${t('untilNext')(fmt(m - n))} (${fmt(m)})` : t('topMilestone');
    panel.append(head, note);
    const byId = new Map(all.map(s => [s.id, s]));
    const months = new Map(), countries = new Map(), cats = new Map();
    for (const [id, iso] of Object.entries(seen.sites)) {
      months.set(monthKey(iso), (months.get(monthKey(iso)) || 0) + 1);
      const s = byId.get(id); if (!s) continue;
      countries.set(s.country_code || '', (countries.get(s.country_code || '') || 0) + 1);
      for (const r of s.roots) cats.set(r, (cats.get(r) || 0) + 1);
    }
    const sortDesc = mp => [...mp].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1));
    const sec = (title, node) => { const h3 = mk('h3'); h3.textContent = title; panel.append(h3, node); };
    sec(t('byMonth'), breakdown([...months].sort((a, b) => (a[0] < b[0] ? 1 : -1)).slice(0, 6), k => k));
    if (countries.size) sec(t('byCountry'), breakdown(sortDesc(countries).slice(0, 6), k => (window.MISE_COUNTRIES && window.MISE_COUNTRIES[k] ? window.MISE_COUNTRIES[k][language] : t('noCountry'))));
    if (cats.size) sec(t('byCat'), breakdown(sortDesc(cats).slice(0, 6), k => { const c = categories.find(x => x.id === k); return c ? c[language === 'ja' ? 'name_ja' : 'name_en'] : k; }));
    const acts = mk('div', 'f-panel-acts');
    const ex = mk('button', '', { type: 'button' }); ex.textContent = t('exportJson');
    ex.addEventListener('click', () => { const a = mk('a', '', { download: 'mise-seen.json', href: URL.createObjectURL(new Blob([JSON.stringify(seen, null, 1)], { type: 'application/json' })) }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000); });
    const im = mk('button', '', { type: 'button' }); im.textContent = t('importJson');
    const file = mk('input', 'f-file', { type: 'file', accept: 'application/json,.json', hidden: '' });
    im.addEventListener('click', () => file.click());
    file.addEventListener('change', async () => {
      const msg = $('.f-panel-msg');
      try {
        const f = file.files[0]; if (!f || f.size > L.SEEN_MAX_BYTES) throw Error('bad');
        const incoming = L.parseSeenImport(await f.text()); if (!incoming) throw Error('bad');
        let add = 0; for (const [k, v] of Object.entries(incoming)) { if (!seen.sites[k] || v < seen.sites[k]) { if (!seen.sites[k]) add++; seen.sites[k] = v; } }
        storage.set(SEEN_KEY, JSON.stringify(seen)); updateSeenBadge(); renderPanel(); $('.f-panel-msg').textContent = t('importOk')(add);
      } catch (_) { msg.textContent = t('importBad'); }
    });
    acts.append(ex, im, file); const msg = mk('p', 'f-panel-msg', { role: 'status' }); const foot = mk('p', 'f-panel-note'); foot.textContent = t('seenNote');
    panel.append(acts, msg, foot);
  }
  function openPanel(button) { const p = $('.f-panel'); p.hidden = false; seenTrigger = button; button.setAttribute('aria-expanded', 'true'); renderPanel(); $('.f-panel-x').focus(); }
  function closePanel() { const p = $('.f-panel'); if (p.hidden) return; p.hidden = true; seenTrigger.setAttribute('aria-expanded', 'false'); seenTrigger.focus(); }

  // ---- 骨格 ----
  function mk(tag, cls, attrs = {}) { const e = document.createElement(tag); if (cls) e.className = cls; for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }
  function seenButton() {
    const button = mk('button', 'f-seen', { type: 'button', 'aria-expanded': 'false', 'aria-controls': 'f-panel' });
    button.innerHTML = '<span class="f-seen-label"></span> <span class="f-seen-num"></span><span class="f-seen-bar" aria-hidden="true"><i></i></span><span class="f-kittens" aria-hidden="true"></span>';
    $('.f-seen-label', button).textContent = t('seen');
    $('.f-kittens', button).innerHTML = kittensHtml();
    button.addEventListener('click', () => ($('.f-panel').hidden ? openPanel(button) : closePanel()));
    return button;
  }

  const directoryHref = () => {
    const url = new URL(globalThis.MiseFeedEntry.directoryHref(scope, path, startPage), location.origin);
    if (filterQuery) url.searchParams.set('q', filterQuery);
    filterTags.forEach(tag => url.searchParams.append('tag', tag));
    if (filtered && startPage > 1) url.searchParams.set('page', String(startPage));
    return url.pathname + url.search;
  };
  function filteredContinueHref() {
    const offset = (startPage - 1) * globalThis.MiseFeedEntry.PAGE_SIZE + loadedCount;
    const url = new URL(directoryHref(), location.origin);
    url.searchParams.set('page', String(Math.floor(offset / globalThis.MiseFeedEntry.PAGE_SIZE) + 1));
    return url.pathname + url.search + '#site-row-' + (offset + 1);
  }

  function renderBar() {
    const back = mk('a', 'f-directory', { href: directoryHref(), title: t('directoryTitle'), 'aria-label': t('directoryTitle') });
    back.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24" width="22" height="22"><path d="m12 5-7 7 7 7M5 12h14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    back.addEventListener('click', event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      // 同じ一覧から来たときは履歴を戻り、読み込み件数とスクロール位置を復元する。
      if (document.referrer === back.href && performance.getEntriesByType('navigation')[0]?.type === 'navigate' && history.length > 1) {
        event.preventDefault(); history.back();
      }
    });
    // 検索欄は通常画面(#search-form)と同じ作り: 虫眼鏡・空白の placeholder(見える文言なし)・隠しラベル・クリアボタン(#214)
    const search = mk('form', 'f-search', { role: 'search', method: 'get', action: globalThis.MiseFeedEntry.directoryHref(scope) });
    const label = mk('label', 'visually-hidden', { for: 'f-query' }); label.textContent = t('searchLabel');
    const icon = mk('span', 'f-search-icon', { 'aria-hidden': 'true' });
    icon.innerHTML = '<svg viewBox="0 -1.1 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/></svg>';
    const input = mk('input', '', { id: 'f-query', type: 'search', name: 'q', size: '25', autocomplete: 'off', placeholder: ' ', required: '' });
    const clear = mk('button', 'f-search-clear', { type: 'button', 'aria-label': t('clearSearch') });
    clear.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    clear.addEventListener('click', () => { input.value = ''; input.focus(); });
    search.append(label, icon, input, clear);
    bar.append(back, search, surfCounter());
    updateSeenBadge();
  }

  // ---- 右上の猫サーフィンと「見たサイト」数(#198)。数字は #58 の累計(markSeen)と同じ値。押すと詳細パネル ----
  const surfSvg = '<svg class="f-surf-cat" viewBox="0 0 40 28" width="40" height="28" aria-hidden="true"><path d="M3 24c3-2 6-2 9 0s6 2 9 0 6-2 9 0 4 1 6 0" fill="none" stroke="#7fd6e6" stroke-width="2" stroke-linecap="round"/><g class="f-surf-rig"><path d="M6 21h28c0 2-2 3-4 3H10c-2 0-4-1-4-3z" fill="#e8b44a"/><path d="M11 9 14 4l3 4z" fill="#df7f3e"/><path d="M25 9 22 4l-3 4z" fill="#252c30"/><path d="M11 8h14v10c0 2-3 3-7 3s-7-1-7-3z" fill="#fff3df"/><path d="M11 8h7l-2 6h-5z" fill="#df7f3e"/><path d="M18 8h7v5h-4z" fill="#252c30"/><circle cx="15" cy="14" r="1.1" fill="#17252b"/><circle cx="22" cy="14" r="1.1" fill="#17252b"/><path d="M29 14c3-1 5 1 4 4" fill="none" stroke="#df7f3e" stroke-width="2" stroke-linecap="round"/></g></svg>';
  function surfCounter() {
    const b = mk('button', 'f-surf', { type: 'button', 'aria-expanded': 'false', 'aria-controls': 'f-panel' });
    b.innerHTML = surfSvg + '<span class="f-surf-text"><span class="f-surf-label"></span><span class="f-surf-num"></span></span>';
    $('.f-surf-label', b).textContent = t('seen');
    b.addEventListener('click', () => ($('.f-panel').hidden ? openPanel(b) : closePanel()));
    for (const el of [$('.f-surf-rig', b), $('.f-surf-num', b)]) el.addEventListener('animationend', () => el.classList.remove('is-hop'));
    return b;
  }
  // 増えるたびに猫が板の上で跳ね、数字がふくらむ。reduced-motion では何もしない(数字の更新だけ)
  function surfHop() {
    if (reduced.matches) return;
    for (const el of [$('.f-surf-rig'), $('.f-surf-num')]) { if (!el) continue; el.classList.remove('is-hop'); void el.getBoundingClientRect(); el.classList.add('is-hop'); }
  }

  // 描画は最初の FIRST_BATCH 枚だけ先に行い、残りは少しずつ足す(1,000 枚を一度に描かない)。終了カードは常に最後に置く
  const FIRST_BATCH = 100, BATCH = 100;
  let rendered = 0, endEl = null, chunkTimer = 0;
  const totalCards = () => (list.length ? list.length + 1 : 1);
  function renderMore(upTo) {
    if (!endEl) return;
    const target = Math.min(list.length, Math.max(upTo, rendered)), fresh = [], frag = document.createDocumentFragment();
    for (let i = rendered; i < target; i++) { const c = card(list[i], i, list.length); fresh.push(c); frag.append(c); }
    if (!fresh.length) return;
    scroller.insertBefore(frag, endEl); cards.splice(rendered, 0, ...fresh); fresh.forEach(c => observer.observe(c)); rendered = target;
  }
  function resetList() {
    observer && observer.disconnect(); dwell.clearAll(); clearTimeout(chunkTimer);
    scroller.replaceChildren(); scroller.classList.remove('f-error'); cards = []; rendered = 0; endEl = null; current = 0;
  }
  function showLoading() {
    resetList(); list = [];
    const p = mk('p', 'f-loading', { role: 'status' }); p.textContent = t('loading') + '…'; scroller.className = 'f-scroll f-error'; scroller.append(p); updateButtons();
  }
  function renderList() {
    list = visible(); resetList(); scroller.className = 'f-scroll';
    if (!list.length) { const e = emptyCard(); scroller.append(e); cards = [e]; }
    else {
      endEl = endCard(list.length); scroller.append(endEl); cards = [endEl]; observer.observe(endEl);
      renderMore(FIRST_BATCH);
      const tick = () => { if (rendered >= list.length) return; renderMore(rendered + BATCH); chunkTimer = setTimeout(tick, 40); };
      chunkTimer = setTimeout(tick, 250);
    }
    scroller.scrollTop = 0;
    updateSeenBadge(); updateButtons();
  }
  // ---- 移動 ----
  function go(i, instant) {
    if (!cards.length) return;
    i = Math.max(0, Math.min(totalCards() - 1, i));
    if (i >= rendered && list.length) renderMore(i >= list.length ? list.length : i + 1); // まだ描いていないカードへ飛ぶときは、そこまで先に描く
    const c = i >= list.length && list.length ? cards[cards.length - 1] : cards[i];
    c.scrollIntoView({ behavior: instant || reduced.matches ? 'auto' : 'smooth', block: 'start' });
    return c;
  }
  // 連打でも取りこぼさないよう、スクロールが落ち着く前は「向かっている先」を基準にする
  let navTarget = null, navTimer = 0;
  function step(d, focus) { const base = navTarget ?? current; navTarget = Math.max(0, Math.min(totalCards() - 1, base + d)); clearTimeout(navTimer); navTimer = setTimeout(() => { navTarget = null; }, 900); const c = go(navTarget); if (c && focus) c.focus({ preventScroll: true }); }
  function updateButtons() { const p = $('#f-prev'), n = $('#f-next'); if (!p) return; p.disabled = current <= 0; n.disabled = !cards.length || !list.length || current >= totalCards() - 1; }

  // ---- 地球連動 ----
  function fly(site) {
    clearTimeout(globeTimer);
    globeTimer = setTimeout(() => {
      const country = site && window.MISE_COUNTRIES && window.MISE_COUNTRIES[site.country_code];
      if (!country) { if (lastPlace) { lastPlace = ''; window.dispatchEvent(new CustomEvent('mise-fly-release')); } return; }
      const region = regionInfo(site), target = region || country, key = region ? site.region_code : site.country_code;
      lastPlace = key;
      window.dispatchEvent(new CustomEvent('mise-fly-country', { detail: { lat: target.lat, lon: target.lon, hold: true, level: region ? 'region' : 'country' } }));
    }, GLOBE_DEBOUNCE_MS);
  }

  function start() {
    if (!document.documentElement.classList.contains('feed-mode') || !globalThis.MiseFeedEntry) return; // フックが通常表示へ戻した後（読み込み失敗・遅延到着）は何も起動しない
    const { PAGE_SIZE, parsePage, scopeFromPath, validScope } = globalThis.MiseFeedEntry;
    const legacyScope = params.get('scope')?.toLowerCase();
    scope = !requestedPath.length && validScope(legacyScope) ? legacyScope : scopeFromPath(location.pathname);
    startPage = requestedPath.length || filtered ? parsePage(params.get('page')) ?? 1 : 1;
    document.documentElement.lang = language;
    root = mk('div', 'feed-root'); root.id = 'feed';
    bar = mk('header', 'f-bar'); bar.setAttribute('role', 'banner');
    scroller = mk('div', 'f-scroll', { role: 'feed', 'aria-label': t('feedLabel'), tabindex: '-1' });
    const nav = mk('div', 'f-nav');
    const prev = mk('button', '', { type: 'button', id: 'f-prev', 'aria-label': t('prev'), title: `${t('prev')} (k)` }), next = mk('button', '', { type: 'button', id: 'f-next', 'aria-label': t('next'), title: `${t('next')} (j)` });
    prev.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20"><path d="m6 15 6-6 6 6" fill="none" stroke="currentColor" stroke-width="2"/></svg>'; next.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20"><path d="m6 9 6 6 6-6" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
    prev.addEventListener('click', () => step(-1)); next.addEventListener('click', () => step(1));
    nav.append(prev, next);
    const hint = mk('p', 'f-hint'); hint.setAttribute('aria-hidden', 'true'); hint.textContent = t('hint');
    const panel = mk('div', 'f-panel', { id: 'f-panel', role: 'region', 'aria-label': t('seen'), hidden: '' });
    path = requestedPath;
    root.append(bar, scroller, nav, hint, panel); renderBar(); new ResizeObserver(() => root.style.setProperty('--bar-h', bar.offsetHeight + 'px')).observe(bar); new ResizeObserver(() => { clearIdlePan(); panFit(activeHeroes()); }).observe(scroller); document.body.append(root);
    scroller.addEventListener('scroll', () => window.dispatchEvent(new Event('mise-globe-busy')), { passive: true }); // スクロール中は背面の地球の描画を休ませる(#196)
    observer = new IntersectionObserver(entries => {
      for (const e of entries) if (e.isIntersecting) {
        const idx = e.target.dataset.index; current = idx === 'end' ? list.length : Number(idx); if (navTarget === current) navTarget = null;
        updateButtons(); fly(idx === 'end' ? null : list[current]);
      }
      for (const e of entries) {
        const idx = e.target.dataset.index; if (idx === 'end') continue;
        e.target.classList.toggle('is-active', e.isIntersecting);
        if (e.isIntersecting) { const h = e.target.querySelector('.f-hero'); if (h) panFit([h]); }
        dwell.stop(e.target);
        if (e.isIntersecting) dwell.start(e.target, list[Number(idx)]);
      }
    }, { root: scroller, threshold: IN_VIEW_THRESHOLD });
    loadSeen();
    showLoading();
    addEventListener('keydown', e => {
      if (e.key === 'Escape' && $('.f-panel') && !$('.f-panel').hidden) { closePanel(); return; }
      if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest('input, textarea, select, [contenteditable]')) return;
      if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); step(1, true); }
      else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); step(-1, true); }
    });
    addEventListener('pagehide', () => window.dispatchEvent(new CustomEvent('mise-fly-release')));
    // 選んだフォルダを API 側で絞り、他フォルダが上限の 500 件に混ざらないようにする。
    async function loadSites() {
      const rows = [], query = new URLSearchParams({ scope });
      if (requestedPath.length && path.length) query.set('category', path.at(-1));
      if (filterQuery) query.set('q', filterQuery);
      filterTags.forEach(tag => query.append('tag', tag));
      const pagedEntry = (path.length || filtered) && startPage > 1;
      if (pagedEntry) query.set('page', String(startPage));
      for (let i = 0; rows.length < MAX_SITES; i++) {
        // page は API の limit を単位にする。最初は箇条書きの 40 件、以降はカーソルで最大 100 件ずつ読む。
        query.set('limit', String(pagedEntry && i === 0 ? PAGE_SIZE : Math.min(100, MAX_SITES - rows.length)));
        try {
          const response = await fetch('/api/sites?' + query);
          if (!response.ok) throw Error('API');
          const data = await response.json();
          rows.push(...data.items);
          hasMore = !!data.next_cursor;
          if (!hasMore) break;
          query.delete('page');
          query.set('cursor', data.next_cursor);
        } catch (e) {
          if (i === 0) throw e;
          loadFailed = true;
          break;
        }
      }
      loadedCount = rows.length;
      return rows;
    }
    fetch('/api/categories').then(async response => {
      if (!response.ok) throw Error('API');
      categories = [globalThis.MiseFeedEntry.recentFolder, ...await response.json()];
      path = resolveCategoryPath();
      $('.f-directory', bar).href = directoryHref();
      all = build(await loadSites());
      renderList();
      if (document.activeElement === document.body) scroller.focus({ preventScroll: true });
      // globe.js は load 後に遅延読込されるため、最初のカードの向き移動を一度だけ遅れて再送する
      setTimeout(() => { if (list[current]) { lastPlace = ''; fly(list[current]); } }, 2600);
    }).catch(() => { scroller.className = 'f-scroll f-error'; scroller.textContent = t('error'); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
