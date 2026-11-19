// 縦フィード（モックアップ・#52）。`?view=feed` のときだけ index.html が読み込む。既定表示には影響しない。
// データは既存の /api/sites をそのまま使う（site × category の行なので id で束ねる）。
(() => {
  // ---- 調整できる定数（触って決める） -------------------------------------------------
  const FRESH_BOOST = 1.5;       // 更新が新しいサイトの出やすさの上乗せ。0=完全ランダム、大きいほど新しい順に近づく
  const FRESH_HALF_LIFE_DAYS = 60; // 更新から何日で優遇が半分になるか
  const GLOBE_DEBOUNCE_MS = 280;   // 速くスクロールしている間は地球を動かさない
  const IN_VIEW_THRESHOLD = 0.6;   // カードがどれだけ見えたら「いまのカード」とみなすか(「見た」の判定にも使う)
  const SEEN_DWELL_MS = 1500;      // 「見た」= 上の割合以上が画面に出たまま、これだけ経過(素通りは数えない)
  const MAX_SITES = 500;         // 1 回の範囲選択で読み込むサイト数の上限(100 件ずつ取得 = 最大 5 リクエスト)
  const LOAD_DEBOUNCE_MS = 300;  // 範囲を続けて切り替えたとき、最後の 1 回だけ取り直す
  const MILESTONES = [10, 50, 100, 300, 1000]; // 節目
  const SEEN_KEY = 'mise-seen-v1'; // 端末内保存(localStorage)。{v:1, sites:{サイトID: 初めて見た日時ISO}}
  // -------------------------------------------------------------------------------------

  const L = window.MiseFeedLib;
  const $ = (s, r = document) => r.querySelector(s);
  const storage = { get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
    sget(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }, sset(k, v) { try { sessionStorage.setItem(k, v); } catch (_) {} } };
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const words = {
    ja: { seen: '見たサイト', seenTitle: n => `見たサイト: ${n}件(タップで詳細)`, seenClose: '閉じる', untilNext: n => `次の節目まであと ${n}件`, topMilestone: 'すべての節目に到達', byMonth: '月ごと', byCountry: '国別', byCat: 'カテゴリ別', exportJson: 'JSONを書き出す', importJson: 'JSONを読み込む', importOk: n => `${n}件を取り込みました`, importBad: '読み込めませんでした', seenNote: 'この端末だけに保存されています。', noCountry: '国不明',
      directory: '一覧表示', directoryTitle: '従来のディレクトリ表示へ', scopeLabel: '範囲', all: '全世界', japan: '日本', note: (n, total) => `先頭 ${n} 件を表示中(全 ${total} 件)`, noteFail: '一部の読み込みに失敗', proto: '試作', protoTitle: '試作ビュー(検索エンジンには載りません)', scopeAllLabel: '全世界', allOf: n => `${n}全域`, catLabel: 'カテゴリ', catAll: 'すべてのカテゴリ', sortLabel: '並び', mix: 'おまかせ', newest: '新しい順', open: 'サイトを開く', more: 'もっと見る', less: '閉じる', next: '次へ', prev: '前へ', updated: '更新', added: '登録', theme: 'ダーク/ライト', lang: 'English', extOn: '外部リンクを別タブで開く: オン', extOff: '外部リンクを別タブで開く: オフ', end: n => `ここまでです。${n}件、見終わりました。`, endNote: 'ここで終わりにして大丈夫です。', top: '最初に戻る', reshuffle: '並びを変えてもう一度', empty: '条件に合うサイトはありません。', error: 'データを取得できませんでした。再読み込みしてください。', loading: '読み込み中', feedLabel: 'サイトのフィード', hint: 'j / k または ↑ ↓ で移動', fallback: '', itemOf: (i, n) => `${i} / ${n}` },
    en: { seen: 'Sites seen', seenTitle: n => `Sites seen: ${n} (tap for details)`, seenClose: 'Close', untilNext: n => `${n} to the next milestone`, topMilestone: 'All milestones reached', byMonth: 'By month', byCountry: 'By country', byCat: 'By category', exportJson: 'Export JSON', importJson: 'Import JSON', importOk: n => `Imported ${n}`, importBad: 'Could not read that file', seenNote: 'Stored on this device only.', noCountry: 'Unknown',
      directory: 'Directory', directoryTitle: 'Switch to the classic directory view', scopeLabel: 'Scope', all: 'World', japan: 'Japan', note: (n, total) => `Showing the first ${n} of ${total}`, noteFail: 'part of the data failed to load', proto: 'BETA', protoTitle: 'Prototype view (not indexed by search engines)', scopeAllLabel: 'World', allOf: n => `All of ${n}`, catLabel: 'Category', catAll: 'All categories', sortLabel: 'Order', mix: 'Shuffle', newest: 'Newest', open: 'Open site', more: 'Read more', less: 'Close', next: 'Next', prev: 'Previous', updated: 'Updated', added: 'Added', theme: 'Dark / light', lang: '日本語', extOn: 'Open external links in new tabs: on', extOff: 'Open external links in new tabs: off', end: n => `That’s all. You have seen ${n} sites.`, endNote: 'It is fine to stop here.', top: 'Back to the first', reshuffle: 'Reshuffle and go again', empty: 'No sites match.', error: 'Could not load data. Please reload.', loading: 'Loading', feedLabel: 'Site feed', hint: 'j / k or ↑ ↓ to move', fallback: ' (Japanese)', itemOf: (i, n) => `${i} / ${n}` }
  };
  // 通常ページ(app.js)と同じ決め方: 保存値、無ければブラウザの言語
  const savedLang = storage.get('mise-language');
  let language = savedLang === 'ja' || savedLang === 'en' ? savedLang : (((navigator.languages && navigator.languages[0]) || navigator.language || '').toLowerCase().indexOf('ja') === 0 ? 'ja' : 'en');
  let newTab = storage.get('mise-external-new-tab') === 'true';
  const t = k => words[language][k];

  // ---- 決定論の乱数（シード = セッションごと。?seed= で再現） ----
  const { hash32, rngOf, updatedOf } = L;

  const params = new URLSearchParams(location.search);
  let seed = params.get('seed') || storage.sget('mise-feed-seed') || Math.random().toString(36).slice(2, 8);
  storage.sset('mise-feed-seed', seed);
  let seedPinned = !!params.get('seed'); // URL に seed を載せるのは、?seed= で来たときと「並びを変える」を押したあと
  // 範囲の初期値: ?scope= 、無ければパス(/jp/ → jp、/jp/13/ → jp-13)
  const pathScope = (m => m ? (m[2] ? `${m[1]}-${m[2]}` : m[1]).toLowerCase() : null)(location.pathname.match(/^\/([a-z]{2})(?:\/([a-z0-9]{1,3}))?\/?$/i));
  const state = { scope: /^(all|[a-z]{2}(-[a-z0-9]{1,3})?)$/i.test(params.get('scope') || '') ? params.get('scope').toLowerCase() : (pathScope || 'all'), cat: params.get('cat') || '', sort: params.get('sort') === 'new' ? 'new' : 'mix' };

  let loadedScope = null, loadedComplete = false, loadFailed = false, loadAbort = null, loadTimer = 0, changeScope = () => {};
  let categories = [], scopes = { countries: [], regions: [] }, all = [], list = [], current = 0, cards = [];
  // 「見た」の滞在タイマー。一覧を作り直すとき clearAll() で古いタイマーを止める(止めないと見ていないサイトを数える)
  const dwell = L.makeDwell(SEEN_DWELL_MS, site => { if (!document.hidden) markSeen(site); });
  let root, scroller, bar, counter, noteEl, observer, globeTimer = 0, lastPlace = '';

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
  function visible() {
    // 並びは全件で一度決め、絞り込みは順序を保ったまま間引く（条件を戻すと同じ並びに戻る）
    return L.order(all, { sort: state.sort, seed, now: Date.now(), boost: FRESH_BOOST, halfLifeDays: FRESH_HALF_LIFE_DAYS }).filter(s => L.inScope(s, state.scope) && (!state.cat || s.roots.has(state.cat)));
  }

  // ---- 代替ビジュアル（サイトIDから決まる三毛猫の模様 + 頭文字）。thumb_url があればそちらを使う ----
  const SVGNS = 'http://www.w3.org/2000/svg';
  function svgEl(name, attrs) { const e = document.createElementNS(SVGNS, name); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }
  function calico(site, withLetter = true) {
    const rnd = rngOf('calico:' + site.id), svg = svgEl('svg', { viewBox: '0 0 400 240', preserveAspectRatio: 'xMidYMid slice', class: 'calico', 'aria-hidden': 'true', focusable: 'false' });
    svg.append(svgEl('rect', { width: 400, height: 240, class: 'c-base' }));
    const n = 5 + Math.floor(rnd() * 3);
    for (let i = 0; i < n; i++) {
      const dark = i % 3 === 2 || rnd() < .28;
      svg.append(svgEl('ellipse', { cx: (rnd() * 440 - 20).toFixed(1), cy: (rnd() * 280 - 20).toFixed(1), rx: (34 + rnd() * 80).toFixed(1), ry: (24 + rnd() * 56).toFixed(1), transform: `rotate(${(rnd() * 180).toFixed(0)} 200 120)`, class: dark ? 'c-dark' : 'c-orange' }));
    }
    if (!withLetter) return svg;
    svg.append(svgEl('circle', { cx: 200, cy: 120, r: 58, class: 'c-plate' }));
    const txt = svgEl('text', { x: 200, y: 120, class: 'c-letter', 'text-anchor': 'middle', 'dominant-baseline': 'central' });
    txt.textContent = [...(site.name.trim() || '?')][0].toUpperCase();
    svg.append(txt);
    return svg;
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
        img.addEventListener('load', () => img.classList.add('is-in')); img.addEventListener('error', () => layer.replaceChildren(calico(s, layer === hero))); img.src = thumb; layer.append(img);
      } else layer.append(calico(s, layer === hero));
    }
    const body = document.createElement('div'); body.className = 'f-body';
    const h = document.createElement('h2'); h.id = `f-name-${i}`; h.className = 'f-name'; h.textContent = s.name;
    const useEn = language === 'en' && s.description_en, p = document.createElement('p'); p.className = 'f-desc'; p.textContent = useEn ? s.description_en : s.description_ja + t('fallback'); if (language === 'en' && !useEn) p.lang = 'ja';
    body.append(h, p);
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
    const when = (s.source_updated_at || s.registered_at || '').slice(0, 10);
    if (when) { const time = document.createElement('time'); time.className = 'f-date'; time.dateTime = when; time.textContent = `${s.source_updated_at ? t('updated') : t('added')} ${when}`; foot.append(time); }
    // http(s) 以外(javascript: / data: / 相対 / 不正)の URL は『開く』リンクを出さない
    const href = L.safeHttpUrl(s.url);
    if (href) { const a = document.createElement('a'); a.className = 'f-open'; a.href = href; a.textContent = t('open'); a.setAttribute('aria-label', `${t('open')}: ${s.name}`); configureLink(a); foot.append(a); }
    body.append(foot);
    const scrim = document.createElement('div'); scrim.className = 'f-scrim'; scrim.setAttribute('aria-hidden', 'true');
    art.append(pic, scrim, body);
    return art;
  }
  function endCard(n) {
    const sec = document.createElement('section'); sec.className = 'f-card f-end'; sec.tabIndex = -1; sec.dataset.index = 'end'; sec.setAttribute('aria-label', t('end')(n));
    const body = document.createElement('div'); body.className = 'f-end-body';
    const h = document.createElement('h2'); h.textContent = t('end')(n);
    const p = document.createElement('p'); p.textContent = t('endNote');
    const row = document.createElement('div'); row.className = 'f-end-actions';
    const top = document.createElement('button'); top.type = 'button'; top.textContent = t('top'); top.addEventListener('click', () => go(0));
    row.append(top);
    if (state.sort === 'mix') { const re = document.createElement('button'); re.type = 'button'; re.className = 'f-quiet'; re.textContent = t('reshuffle'); re.addEventListener('click', () => { seed = Math.random().toString(36).slice(2, 8); storage.sset('mise-feed-seed', seed); seedPinned = true; syncUrl(); renderList(); go(0, true); }); row.append(re); }
    body.append(h, p, row); sec.append(body);
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
    const n = seenCount(); updateSeenBadge();
    if (!reduced.matches) { const num = $('.f-seen-num'); num && num.animate([{ transform: 'scale(1)' }, { transform: 'scale(1.35)' }, { transform: 'scale(1)' }], { duration: 380, easing: 'ease-out' }); }
    if (MILESTONES.includes(n)) { const kit = $('.f-kittens'); if (kit) { kit.innerHTML = kittensHtml(); const last = kit.lastElementChild;
      if (last && !reduced.matches) { const a = last.animate([{ opacity: 0, transform: 'translateY(-14px) rotate(-20deg)' }, { opacity: 1, transform: 'translateY(2px)' }, { transform: 'translateY(0)' }], { duration: 700, easing: 'ease-out' }); addEventListener('pointerdown', () => a.finish(), { once: true }); } } }
    if ($('.f-panel') && !$('.f-panel').hidden) renderPanel();
  }
  const kittenSvg = i => `<svg class="f-kitten" viewBox="0 0 16 16" width="14" height="14"><path fill="${i % 2 ? '#252c30' : '#df7f3e'}" d="M2 1.5 6 4 3 6.5z"/><path fill="${i % 2 ? '#df7f3e' : '#252c30'}" d="M14 1.5 10 4l3 2.5z"/><path d="M3 5.5h10v6.5c0 1.5-1.6 2.5-5 2.5s-5-1-5-2.5z" fill="#fff3df"/><path d="M3 5.5h4.5l-1 4H3z" fill="${i % 2 ? '#252c30' : '#df7f3e'}"/><circle cx="6" cy="9.5" r=".9" fill="#17252b"/><circle cx="10" cy="9.5" r=".9" fill="#17252b"/></svg>`;
  // 節目に届いた数だけ子猫が並ぶ(10件で1匹、50件で2匹…)。保存データからの導出なので別管理しない
  const kittensHtml = () => MILESTONES.filter(m => m <= seenCount()).map((m, i) => kittenSvg(i)).join('');
  function updateSeenBadge() {
    const btn = $('.f-seen'); if (!btn) return; const n = seenCount(), m = nextMilestone(n);
    $('.f-seen-num', btn).textContent = String(n).padStart(6, '0'); btn.title = btn.ariaLabel = t('seenTitle')(n);
    const bar = $('.f-seen-bar i', btn); if (bar) { const prev = [0, ...MILESTONES].filter(x => x <= n).pop(); bar.style.width = m ? `${Math.round((n - prev) / (m - prev) * 100)}%` : '100%'; }
  }
  function monthKey(iso) { const d = new Date(iso); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; }
  function breakdown(list, label) {
    const ul = mk('ul', 'f-bd'); const max = Math.max(1, ...list.map(x => x[1]));
    for (const [k, v] of list) { const li = mk('li'); const name = mk('span', 'f-bd-k'); name.textContent = label(k); const track = mk('span', 'f-bd-t', { 'aria-hidden': 'true' }); const i = mk('i'); i.style.width = `${v / max * 100}%`; track.append(i); const num = mk('span', 'f-bd-v'); num.textContent = v; li.append(name, track, num); ul.append(li); }
    return ul;
  }
  function renderPanel() {
    const panel = $('.f-panel'); const n = seenCount(), m = nextMilestone(n); panel.replaceChildren();
    const head = mk('div', 'f-panel-head'); const h = mk('h2'); h.textContent = `${t('seen')}: ${n}`; const x = mk('button', 'f-panel-x', { type: 'button', 'aria-label': t('seenClose') }); x.textContent = '×'; x.addEventListener('click', closePanel); head.append(h, x);
    const note = mk('p', 'f-panel-note'); note.textContent = m ? `${t('untilNext')(m - n)} (${m})` : t('topMilestone');
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
  function openPanel() { const p = $('.f-panel'); p.hidden = false; $('.f-seen').setAttribute('aria-expanded', 'true'); renderPanel(); }
  function closePanel() { const p = $('.f-panel'); if (p.hidden) return; p.hidden = true; $('.f-seen').setAttribute('aria-expanded', 'false'); $('.f-seen').focus(); }

  // ---- 骨格 ----
  function mk(tag, cls, attrs = {}) { const e = document.createElement(tag); if (cls) e.className = cls; for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }
  function segment(name, options, value, onPick) {
    const g = mk('div', 'f-seg', { role: 'group', 'aria-label': t(name + 'Label') });
    for (const [v, label] of options) { const b = mk('button', '', { type: 'button', 'aria-pressed': String(v === value) }); b.textContent = label; b.addEventListener('click', () => onPick(v)); g.append(b); }
    return g;
  }
  const counter0 = () => (counter = mk('span', 'f-counter', { 'aria-hidden': 'true' }));
  function renderBar() {
    bar.replaceChildren();
    const row1 = mk('div', 'f-row');
    const logo = mk('a', 'f-logo', { href: location.pathname + '?view=feed', 'aria-label': 'MISE' }); logo.textContent = 'MISE';
    const space = mk('span', 'f-grow');
    const dir = mk('a', 'f-directory', { href: location.pathname, title: t('directoryTitle'), 'aria-label': t('directoryTitle') }); dir.textContent = t('directory');
    const ext = mk('button', 'f-icon', { type: 'button', 'aria-pressed': String(newTab), 'aria-label': t(newTab ? 'extOn' : 'extOff'), title: t(newTab ? 'extOn' : 'extOff') });
    ext.innerHTML = '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16"><path fill="currentColor" d="M3.75 2h3.5a.75.75 0 0 1 0 1.5h-3.5a.25.25 0 0 0-.25.25v8.5c0 .138.112.25.25.25h8.5a.25.25 0 0 0 .25-.25v-3.5a.75.75 0 0 1 1.5 0v3.5A1.75 1.75 0 0 1 12.25 14h-8.5A1.75 1.75 0 0 1 2 12.25v-8.5C2 2.784 2.784 2 3.75 2Zm6.854-1h4.146a.25.25 0 0 1 .25.25v4.146a.25.25 0 0 1-.427.177L13.03 4.03 9.28 7.78a.751.751 0 0 1-1.042-.018.751.751 0 0 1-.018-1.042l3.75-3.75-1.543-1.543A.25.25 0 0 1 10.604 1Z"/></svg>';
    ext.addEventListener('click', () => { newTab = !newTab; storage.set('mise-external-new-tab', String(newTab)); ext.setAttribute('aria-pressed', String(newTab)); ext.title = ext.ariaLabel = t(newTab ? 'extOn' : 'extOff'); document.querySelectorAll('.f-open').forEach(configureLink); });
    // テーマ切替で <meta name="theme-color"> は追従させない(通常ページ(app.js)の仕様に合わせる。試作では触らない)
    const th = mk('button', 'f-icon', { type: 'button', 'aria-label': t('theme'), title: t('theme') }); th.textContent = '◐';
    th.addEventListener('click', () => { const v = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = v; storage.set('mise-theme', v); });
    const lg = mk('button', 'f-icon', { type: 'button', 'aria-label': t('lang'), title: t('lang') }); lg.textContent = '文';
    lg.addEventListener('click', () => { language = language === 'ja' ? 'en' : 'ja'; storage.set('mise-language', language); document.documentElement.lang = language; const id = list[current] && list[current].id; renderBar(); renderList(id); updateCounter(); });
    const sb = mk('button', 'f-seen', { type: 'button', 'aria-expanded': 'false', 'aria-controls': 'f-panel' });
    sb.innerHTML = '<span class="f-seen-label" aria-hidden="true">SEEN</span> <span class="f-seen-num" aria-hidden="true"></span><span class="f-seen-bar" aria-hidden="true"><i></i></span>';
    sb.addEventListener('click', () => ($('.f-panel').hidden ? openPanel() : closePanel()));
    const kit = mk('span', 'f-kittens', { 'aria-hidden': 'true' }); kit.innerHTML = kittensHtml();
    const row0 = mk('div', 'f-row f-row0'); const proto = mk('span', 'f-proto', { title: t('protoTitle') }); proto.textContent = t('proto');
    noteEl = mk('span', 'f-note', { role: 'status' });
    row0.append(counter0(), noteEl, mk('span', 'f-grow'), proto, sb); updateNote();
    row1.append(logo, kit, space, dir, ext, th, lg);

    const row2 = mk('div', 'f-row f-filters');
    const scopeSel = mk('select', 'f-select f-scope', { 'aria-label': t('scopeLabel') });
    scopeSel.append(new Option(t('scopeAllLabel'), 'all'));
    const nameOf = (m, code) => (m && m[code] ? m[code][language] : null);
    for (const c of scopes.countries) {
      const cc = String(c.code).toUpperCase(), cn = nameOf(window.MISE_COUNTRIES, cc) || cc;
      scopeSel.append(new Option(`${cn} (${c.count})`, cc.toLowerCase()));
      for (const r of scopes.regions.filter(r => String(r.code).toUpperCase().startsWith(cc + '-'))) scopeSel.append(new Option(`\u00a0\u00a0${nameOf(window.MISE_REGIONS, String(r.code).toUpperCase()) || r.code} (${r.count})`, String(r.code).toLowerCase()));
    }
    if (![...scopeSel.options].some(o => o.value === state.scope)) state.scope = 'all';
    scopeSel.value = state.scope; scopeSel.addEventListener('change', () => changeScope(scopeSel.value));
    row2.append(scopeSel);
    const sel = mk('select', 'f-select', { 'aria-label': t('catLabel') });
    sel.append(new Option(t('catAll'), ''));
    for (const c of categories.filter(c => !c.parent_id)) { const n = all.filter(s => L.inScope(s, state.scope) && s.roots.has(c.id)).length; if (n) sel.append(new Option(`${c[language === 'ja' ? 'name_ja' : 'name_en']} (${n})`, c.id)); }
    sel.value = state.cat; sel.addEventListener('change', () => { state.cat = sel.value; refilter(); });
    row2.append(sel, mk('span', 'f-grow'), segment('sort', [['mix', t('mix')], ['new', t('newest')]], state.sort, v => { state.sort = v; refilter(); }));
    const panel = mk('div', 'f-panel', { id: 'f-panel', role: 'region', 'aria-label': t('seen') }); panel.hidden = true;
    bar.append(row0, row1, row2, panel); updateSeenBadge();
  }
  function syncUrl() {
    const u = new URL(location.href); u.search = ''; u.searchParams.set('view', 'feed');
    if (state.scope !== 'all' || pathScope) u.searchParams.set('scope', state.scope); if (state.cat) u.searchParams.set('cat', state.cat); if (state.sort !== 'mix') u.searchParams.set('sort', state.sort); if (seedPinned) u.searchParams.set('seed', seed);
    history.replaceState(null, '', u.pathname + u.search);
  }
  function refilter() { syncUrl(); renderBar(); renderList(); go(0, true); }

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
    const p = mk('p', 'f-loading', { role: 'status' }); p.textContent = t('loading') + '…'; scroller.className = 'f-scroll f-error'; scroller.append(p); updateCounter(); updateButtons();
  }
  function renderList(keepId) {
    list = visible(); resetList(); scroller.className = 'f-scroll';
    if (!list.length) { const e = emptyCard(); scroller.append(e); cards = [e]; }
    else {
      endEl = endCard(list.length); scroller.append(endEl); cards = [endEl]; observer.observe(endEl);
      renderMore(FIRST_BATCH);
      const tick = () => { if (rendered >= list.length) return; renderMore(rendered + BATCH); chunkTimer = setTimeout(tick, 40); };
      chunkTimer = setTimeout(tick, 250);
    }
    const at = keepId ? Math.max(0, list.findIndex(s => s.id === keepId)) : 0;
    scroller.scrollTop = 0; if (at) { renderMore(at + 1); requestAnimationFrame(() => go(at, true)); }
    updateCounter(); updateButtons();
  }
  // 範囲の全体件数(/api/scopes)。読み込んだ数がそれより少ないときは、画面に正直に出す
  function scopeTotal() {
    if (state.scope === 'all') return scopes.total;
    const code = state.scope.toUpperCase(), row = (state.scope.includes('-') ? scopes.regions : scopes.countries).find(r => String(r.code).toUpperCase() === code);
    return row ? row.count : undefined;
  }
  function updateNote() {
    if (!noteEl) return;
    const total = scopeTotal(), loaded = all.length;
    noteEl.textContent = loadedScope !== null && ((Number.isFinite(total) && loaded < total) || loadFailed) ? t('note')(loaded, Number.isFinite(total) ? total : loaded) + (loadFailed ? ` / ${t('noteFail')}` : '') : '';
  }
  function updateCounter() { if (counter) counter.textContent = list.length ? t('itemOf')(Math.min(current + 1, list.length), list.length) : ''; }

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
    root.append(bar, scroller, nav, hint); new ResizeObserver(() => root.style.setProperty('--bar-h', bar.offsetHeight + 'px')).observe(bar); document.body.append(root);
    observer = new IntersectionObserver(entries => {
      for (const e of entries) if (e.isIntersecting) {
        const idx = e.target.dataset.index; current = idx === 'end' ? list.length : Number(idx); if (navTarget === current) navTarget = null;
        updateCounter(); updateButtons(); fly(idx === 'end' ? null : list[current]);
      }
      for (const e of entries) {
        const idx = e.target.dataset.index; if (idx === 'end') continue;
        dwell.stop(e.target);
        if (e.isIntersecting) dwell.start(e.target, list[Number(idx)]);
      }
    }, { root: scroller, threshold: IN_VIEW_THRESHOLD });
    loadSeen();
    addEventListener('keydown', e => {
      if (e.key === 'Escape' && $('.f-panel') && !$('.f-panel').hidden) { closePanel(); return; }
      if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest('input, textarea, select, [contenteditable]')) return;
      if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); step(1, true); }
      else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); step(-1, true); }
    });
    addEventListener('pagehide', () => window.dispatchEvent(new CustomEvent('mise-fly-release')));
    // サイトは範囲ごとにサーバーから 100 件ずつ cursor でたどって読む(上限 MAX_SITES。超えた分は出さず、画面に注記する)。
    // 2 ページ目以降の失敗は、読めた分で続ける(注記つき)。1 ページ目の失敗はエラー
    async function loadSites(scope, signal) {
      const rows = []; let cursor = null, failed = false;
      for (let i = 0; i < Math.ceil(MAX_SITES / 100); i++) {
        let j;
        try {
          const r = await fetch('/api/sites?limit=100' + (scope !== 'all' ? '&scope=' + encodeURIComponent(scope) : '') + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''), { signal });
          if (!r.ok) throw Error('API'); j = await r.json();
        } catch (e) { if (signal.aborted || i === 0) throw e; failed = true; break; }
        rows.push(...j.items); cursor = j.next_cursor; if (!cursor) break;
      }
      return { rows, failed, more: !!cursor };
    }
    async function reload(scope) {
      loadAbort && loadAbort.abort(); const ctl = loadAbort = new AbortController();
      showLoading();
      try {
        const res = await loadSites(scope, ctl.signal); if (ctl.signal.aborted) return;
        all = build(res.rows); loadedScope = scope; loadedComplete = !res.more && !res.failed; loadFailed = res.failed;
        if (!categories.some(c => c.id === state.cat && !c.parent_id)) state.cat = '';
        renderBar(); renderList(); updateCounter();
      } catch (e) { if (ctl.signal.aborted) return; scroller.className = 'f-scroll f-error'; scroller.textContent = t('error'); }
    }
    // 範囲の変更。読み込み済みの範囲に含まれる(全件読めている)なら絞り込むだけ。そうでなければ 300ms 待って取り直す(連続操作は 1 回に)
    changeScope = v => {
      state.scope = v; syncUrl(); clearTimeout(loadTimer);
      if (L.covers(loadedScope, loadedComplete, v)) { loadAbort && loadAbort.abort(); if (loadedScope !== null) { renderBar(); renderList(); go(0, true); } return; }
      showLoading(); loadTimer = setTimeout(() => reload(v), LOAD_DEBOUNCE_MS);
    };
    Promise.all([fetch('/api/categories'), fetch('/api/scopes').then(r => r.ok ? r.json() : null).catch(() => null)]).then(async ([a, sc]) => {
      if (!a.ok) throw Error('API');
      if (sc && Array.isArray(sc.countries)) scopes = { total: sc.total, countries: sc.countries, regions: Array.isArray(sc.regions) ? sc.regions : [] };
      categories = await a.json();
      renderBar(); await reload(state.scope); scroller.focus({ preventScroll: true });
      // globe.js は load 後に遅延読込されるため、最初のカードの向き移動を一度だけ遅れて再送する
      setTimeout(() => { if (list[current]) { lastPlace = ''; fly(list[current]); } }, 2600);
    }).catch(() => { scroller.className = 'f-scroll f-error'; scroller.textContent = t('error'); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
