// 縦フィード（モックアップ・#52）。`?view=feed` のときだけ index.html が読み込む。既定表示には影響しない。
// データは既存の /api/sites をそのまま使う（site × category の行なので id で束ねる）。
(() => {
  // ---- 調整できる定数（触って決める） -------------------------------------------------
  const FRESH_BOOST = 1.5;       // 更新が新しいサイトの出やすさの上乗せ。0=完全ランダム、大きいほど新しい順に近づく
  const FRESH_HALF_LIFE_DAYS = 60; // 更新から何日で優遇が半分になるか
  const GLOBE_DEBOUNCE_MS = 280;   // 速くスクロールしている間は地球を動かさない
  const IN_VIEW_THRESHOLD = 0.6;   // カードがどれだけ見えたら「いまのカード」とみなすか
  // -------------------------------------------------------------------------------------

  const $ = (s, r = document) => r.querySelector(s);
  const storage = { get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
    sget(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }, sset(k, v) { try { sessionStorage.setItem(k, v); } catch (_) {} } };
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const words = {
    ja: { directory: '一覧表示', directoryTitle: '従来のディレクトリ表示へ', scopeLabel: '範囲', all: '全世界', japan: '日本', catLabel: 'カテゴリ', catAll: 'すべてのカテゴリ', sortLabel: '並び', mix: 'おまかせ', newest: '新しい順', open: 'サイトを開く', more: 'もっと見る', less: '閉じる', next: '次へ', prev: '前へ', updated: '更新', added: '登録', theme: 'ダーク/ライト', lang: 'English', extOn: '外部リンクを別タブで開く: オン', extOff: '外部リンクを別タブで開く: オフ', end: n => `ここまでです。${n}件、見終わりました。`, endNote: 'ここで終わりにして大丈夫です。', top: '最初に戻る', reshuffle: '並びを変えてもう一度', empty: '条件に合うサイトはありません。', error: 'データを取得できませんでした。再読み込みしてください。', loading: '読み込み中', feedLabel: 'サイトのフィード', hint: 'j / k または ↑ ↓ で移動', fallback: '', itemOf: (i, n) => `${i} / ${n}` },
    en: { directory: 'Directory', directoryTitle: 'Switch to the classic directory view', scopeLabel: 'Scope', all: 'World', japan: 'Japan', catLabel: 'Category', catAll: 'All categories', sortLabel: 'Order', mix: 'Shuffle', newest: 'Newest', open: 'Open site', more: 'Read more', less: 'Close', next: 'Next', prev: 'Previous', updated: 'Updated', added: 'Added', theme: 'Dark / light', lang: '日本語', extOn: 'Open external links in new tabs: on', extOff: 'Open external links in new tabs: off', end: n => `That’s all. You have seen ${n} sites.`, endNote: 'It is fine to stop here.', top: 'Back to the first', reshuffle: 'Reshuffle and go again', empty: 'No sites match.', error: 'Could not load data. Please reload.', loading: 'Loading', feedLabel: 'Site feed', hint: 'j / k or ↑ ↓ to move', fallback: ' (Japanese)', itemOf: (i, n) => `${i} / ${n}` }
  };
  let language = storage.get('mise-language') === 'ja' ? 'ja' : 'en';
  let newTab = storage.get('mise-external-new-tab') === 'true';
  const t = k => words[language][k];

  // ---- 決定論の乱数（シード = セッションごと。?seed= で再現） ----
  function hash32(str) { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function rngOf(seedStr) { let a = hash32(String(seedStr)); return () => { a = (a + 0x6D2B79F5) | 0; let x = Math.imul(a ^ (a >>> 15), 1 | a); x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x; return ((x ^ (x >>> 14)) >>> 0) / 4294967296; }; }

  const params = new URLSearchParams(location.search);
  let seed = params.get('seed') || storage.sget('mise-feed-seed') || Math.random().toString(36).slice(2, 8);
  storage.sset('mise-feed-seed', seed);
  const state = { scope: params.get('scope') === 'jp' ? 'jp' : 'all', cat: params.get('cat') || '', sort: params.get('sort') === 'new' ? 'new' : 'mix' };

  let categories = [], all = [], list = [], current = 0, cards = [];
  let root, scroller, bar, counter, observer, globeTimer = 0, lastPlace = '';

  // ---- データ整形 ----
  const updatedOf = s => Date.parse(s.source_updated_at || s.registered_at);
  function rootCat(id) { let n = categories.find(c => c.id === id); while (n && n.parent_id) n = categories.find(c => c.id === n.parent_id); return n || null; }
  function build(rows) {
    const map = new Map();
    for (const r of rows) {
      let s = map.get(r.id);
      if (!s) { s = { ...r, roots: new Set(), cats: new Set() }; delete s.category_id; map.set(r.id, s); }
      s.cats.add(r.category_id); const rc = rootCat(r.category_id); if (rc) s.roots.add(rc.id);
    }
    return [...map.values()];
  }
  // 重み付きランダム順（Efraimidis-Spirakis）。key = u^(1/w) の降順。w = 1 + FRESH_BOOST * 0.5^(経過日/半減期)
  function order(sites) {
    if (state.sort === 'new') return [...sites].sort((a, b) => (updatedOf(b) || 0) - (updatedOf(a) || 0) || (a.id < b.id ? -1 : 1));
    const rnd = rngOf(seed), now = Date.now();
    return sites.map(s => {
      const days = Math.max(0, (now - (updatedOf(s) || 0)) / 86400000), fresh = Number.isFinite(updatedOf(s)) ? Math.pow(.5, days / FRESH_HALF_LIFE_DAYS) : 0;
      return { s, key: Math.pow(rnd(), 1 / (1 + FRESH_BOOST * fresh)) };
    }).sort((a, b) => b.key - a.key || (a.s.id < b.s.id ? -1 : 1)).map(x => x.s);
  }
  function visible() {
    // 並びは全件で一度決め、絞り込みは順序を保ったまま間引く（条件を戻すと同じ並びに戻る）
    return order(all).filter(s => (state.scope === 'all' || s.country_code === 'JP') && (!state.cat || s.roots.has(state.cat)));
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
  const thumbOf = s => (typeof s.thumb_url === 'string' && /^(\/|https:\/\/)/.test(s.thumb_url)) ? s.thumb_url : null;

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
  function longText(s) {
    const own = language === 'en' ? s.long_description_en : s.long_description_ja;
    if (own) return { text: own, mock: false };
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
    const lt = longText(s), cap = document.createElement('div'); cap.className = 'f-cap'; cap.dataset.mock = lt.mock ? 'true' : 'false';
    const lp = document.createElement('p'); lp.className = 'f-long'; lp.id = `f-long-${i}`; lp.textContent = lt.text;
    if (language === 'en' && !s.long_description_en && !lt.mock) lp.lang = 'ja';
    const more = document.createElement('button'); more.type = 'button'; more.className = 'f-more'; more.setAttribute('aria-expanded', 'false'); more.setAttribute('aria-controls', lp.id); more.textContent = t('more');
    const toggle = () => { const on = cap.classList.toggle('is-open'); more.setAttribute('aria-expanded', String(on)); more.textContent = t(on ? 'less' : 'more'); lp.tabIndex = on ? 0 : -1; if (!on) lp.scrollTop = 0; };
    more.addEventListener('click', toggle);
    lp.addEventListener('click', () => { if (String(getSelection()).length) return; toggle(); });
    cap.append(lp, more); body.append(cap);
    let tags = []; try { tags = JSON.parse(s.tags_json || '[]'); } catch (_) {}
    if (tags.length) { const ul = document.createElement('ul'); ul.className = 'f-tags'; for (const tg of tags) { const li = document.createElement('li'); li.textContent = (language === 'en' && tg.en) ? tg.en : tg.ja; ul.append(li); } body.append(ul); }
    const foot = document.createElement('div'); foot.className = 'f-foot';
    const when = (s.source_updated_at || s.registered_at || '').slice(0, 10);
    if (when) { const time = document.createElement('time'); time.className = 'f-date'; time.dateTime = when; time.textContent = `${s.source_updated_at ? t('updated') : t('added')} ${when}`; foot.append(time); }
    const a = document.createElement('a'); a.className = 'f-open'; a.href = s.url; a.textContent = t('open'); a.setAttribute('aria-label', `${t('open')}: ${s.name}`); configureLink(a); foot.append(a);
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
    if (state.sort === 'mix') { const re = document.createElement('button'); re.type = 'button'; re.className = 'f-quiet'; re.textContent = t('reshuffle'); re.addEventListener('click', () => { seed = Math.random().toString(36).slice(2, 8); storage.sset('mise-feed-seed', seed); renderList(); go(0, true); }); row.append(re); }
    body.append(h, p, row); sec.append(body);
    return sec;
  }
  function emptyCard() {
    const sec = document.createElement('section'); sec.className = 'f-card f-end'; const b = document.createElement('div'); b.className = 'f-end-body'; const h = document.createElement('h2'); h.textContent = t('empty'); b.append(h); sec.append(b); return sec;
  }

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
    const th = mk('button', 'f-icon', { type: 'button', 'aria-label': t('theme'), title: t('theme') }); th.textContent = '◐';
    th.addEventListener('click', () => { const v = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = v; storage.set('mise-theme', v); });
    const lg = mk('button', 'f-icon', { type: 'button', 'aria-label': t('lang'), title: t('lang') }); lg.textContent = '文';
    lg.addEventListener('click', () => { language = language === 'ja' ? 'en' : 'ja'; storage.set('mise-language', language); document.documentElement.lang = language; const id = list[current] && list[current].id; renderBar(); renderList(id); updateCounter(); });
    row1.append(logo, counter0(), space, dir, ext, th, lg);

    const row2 = mk('div', 'f-row f-filters');
    row2.append(segment('scope', [['all', t('all')], ['jp', t('japan')]], state.scope, v => { state.scope = v; refilter(); }));
    const sel = mk('select', 'f-select', { 'aria-label': t('catLabel') });
    sel.append(new Option(t('catAll'), ''));
    for (const c of categories.filter(c => !c.parent_id)) { const n = new Set(all.filter(s => s.roots.has(c.id)).map(s => s.id)).size; if (n) sel.append(new Option(`${c[language === 'ja' ? 'name_ja' : 'name_en']} (${n})`, c.id)); }
    sel.value = state.cat; sel.addEventListener('change', () => { state.cat = sel.value; refilter(); });
    row2.append(sel, mk('span', 'f-grow'), segment('sort', [['mix', t('mix')], ['new', t('newest')]], state.sort, v => { state.sort = v; refilter(); }));
    bar.append(row1, row2);
  }
  function syncUrl() {
    const u = new URL(location.href); u.search = ''; u.searchParams.set('view', 'feed');
    if (state.scope !== 'all') u.searchParams.set('scope', state.scope); if (state.cat) u.searchParams.set('cat', state.cat); if (state.sort !== 'mix') u.searchParams.set('sort', state.sort); if (params.get('seed')) u.searchParams.set('seed', params.get('seed'));
    history.replaceState(null, '', u.pathname + u.search);
  }
  function refilter() { syncUrl(); renderBar(); renderList(); go(0, true); }

  function renderList(keepId) {
    list = visible();
    observer && observer.disconnect();
    scroller.replaceChildren();
    cards = list.map((s, i) => card(s, i, list.length));
    scroller.append(...(cards.length ? [...cards, endCard(list.length)] : [emptyCard()]));
    cards = [...scroller.querySelectorAll('.f-card')];
    cards.forEach(c => observer.observe(c));
    const at = keepId ? Math.max(0, list.findIndex(s => s.id === keepId)) : 0;
    current = at; scroller.scrollTop = 0; if (at) requestAnimationFrame(() => go(at, true));
    updateCounter(); updateButtons();
  }
  function updateCounter() { if (counter) counter.textContent = list.length ? t('itemOf')(Math.min(current + 1, list.length), list.length) : ''; }

  // ---- 移動 ----
  function go(i, instant) {
    if (!cards.length) return;
    i = Math.max(0, Math.min(cards.length - 1, i));
    cards[i].scrollIntoView({ behavior: instant || reduced.matches ? 'auto' : 'smooth', block: 'start' });
    return cards[i];
  }
  function step(d, focus) { const c = go(current + d); if (c && focus) c.focus({ preventScroll: true }); }
  function updateButtons() { const p = $('#f-prev'), n = $('#f-next'); if (!p) return; p.disabled = current <= 0; n.disabled = !cards.length || current >= cards.length - 1; }

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
        const idx = e.target.dataset.index; current = idx === 'end' ? list.length : Number(idx);
        updateCounter(); updateButtons(); fly(idx === 'end' ? null : list[current]);
      }
    }, { root: scroller, threshold: IN_VIEW_THRESHOLD });
    addEventListener('keydown', e => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest('input, textarea, select, [contenteditable]')) return;
      if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); step(1, true); }
      else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); step(-1, true); }
    });
    addEventListener('pagehide', () => window.dispatchEvent(new CustomEvent('mise-fly-release')));
    Promise.all([fetch('/api/categories'), fetch('/api/sites')]).then(async ([a, b]) => {
      if (!a.ok || !b.ok) throw Error('API');
      categories = await a.json(); all = build(await b.json());
      if (!categories.some(c => c.id === state.cat && !c.parent_id)) state.cat = '';
      renderBar(); renderList(); updateCounter(); scroller.focus({ preventScroll: true });
      // globe.js は load 後に遅延読込されるため、最初のカードの向き移動を一度だけ遅れて再送する
      setTimeout(() => { if (list[current]) { lastPlace = ''; fly(list[current]); } }, 2600);
    }).catch(() => { scroller.textContent = t('error'); scroller.className += ' f-error'; });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
