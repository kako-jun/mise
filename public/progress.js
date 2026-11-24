(() => {
  let savedLanguage = null; try { savedLanguage = localStorage.getItem('mise-language'); } catch {}
  const lang = (savedLanguage || navigator.language || 'en').startsWith('ja') ? 'ja' : 'en';
  document.documentElement.lang = lang;
  document.querySelectorAll('[data-ja][data-en]').forEach(e => { e.textContent = e.dataset[lang]; });
  document.querySelectorAll('[data-ja-placeholder][data-en-placeholder]').forEach(e => { e.placeholder = e.dataset[`${lang}Placeholder`]; });
  document.querySelectorAll('[data-ja-label][data-en-label]').forEach(e => { e.setAttribute('aria-label', e.dataset[`${lang}Label`]); });

  const tbody = document.querySelector('#rows'), form = document.querySelector('#filters');
  const queryInput = document.querySelector('#query'), sourceSelect = document.querySelector('#source');
  const stateSelect = document.querySelector('#state'), missingSelect = document.querySelector('#missing');
  const sizes = [...document.querySelectorAll('.page-size-select')], pages = [...document.querySelectorAll('.page-count')];
  const pageInputs = [...document.querySelectorAll('.page-input')], pageButtons = [...document.querySelectorAll('[data-page-action]')];
  const validStates = ['all','discovered','queued','fetched','candidate','reviewing','approved','rejected','failed','blocked','published'];
  const validSources = ['all','discovered','request'], validMissing = ['all','favicon','thumbnail','media'], validLimits = [25,50,100];
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const cell = (ok, yes, no='…') => `<td class="${ok ? 'done' : 'wait'}">${ok ? yes : no}</td>`;
  const states = {discovered:0,queued:1,fetched:2,candidate:3,reviewing:4,approved:5,rejected:5,failed:2,blocked:2,published:8};
  const done = (ok, dash=false) => cell(ok, '✓', dash ? '—' : '…');
  let total = 0, page = 1, pageSize = 50, loadSeq = 0;
  let filters = { q:'', source:'all', state:'all', missing:'all' };

  function readUrl() {
    const p = new URLSearchParams(location.search), lim = Number(p.get('limit'));
    filters = { q:(p.get('q') || '').slice(0,100), source:validSources.includes(p.get('source')) ? p.get('source') : 'all', state:validStates.includes(p.get('state')) ? p.get('state') : 'all', missing:validMissing.includes(p.get('missing')) ? p.get('missing') : 'all' };
    pageSize = validLimits.includes(lim) ? lim : 50;
    page = Math.max(1, Math.min(Math.floor(1000000 / pageSize) + 1, Math.trunc(Number(p.get('page')) || 1)));
    syncForm();
  }
  function syncForm() {
    queryInput.value = filters.q; sourceSelect.value = filters.source; stateSelect.value = filters.state; missingSelect.value = filters.missing;
    sizes.forEach(s => { s.value = String(pageSize); }); pageInputs.forEach(i => { i.value = String(page); });
  }
  function updateUrl(push) {
    const url = new URL(location.href), p = new URLSearchParams();
    if (filters.q) p.set('q', filters.q);
    if (filters.source !== 'all') p.set('source', filters.source);
    if (filters.state !== 'all') p.set('state', filters.state);
    if (filters.missing !== 'all') p.set('missing', filters.missing);
    if (pageSize !== 50) p.set('limit', String(pageSize));
    if (page !== 1) p.set('page', String(page));
    url.search = p.toString();
    history[push ? 'pushState' : 'replaceState']({}, '', url);
  }
  function updatePager() {
    const pageTotal = Math.max(1, Math.ceil(total / pageSize));
    pageInputs.forEach(i => { i.value = String(page); i.max = String(pageTotal); });
    pages.forEach(e => { e.textContent = pageTotal.toLocaleString(); });
    pageButtons.forEach(b => {
      const action = b.dataset.pageAction;
      b.disabled = (action === 'first' || action === 'prev') ? page <= 1 : (action === 'next' || action === 'last') ? page >= pageTotal : false;
    });
  }
  async function load(push=false) {
    const seq = ++loadSeq;
    updateUrl(push);
    tbody.innerHTML = `<tr><td colspan="12">${lang === 'ja' ? '読み込み中…' : 'Loading…'}</td></tr>`;
    try {
      const p = new URLSearchParams({limit:String(pageSize),offset:String((page-1)*pageSize),source:filters.source,state:filters.state,missing:filters.missing});
      if (filters.q) p.set('q', filters.q);
      const r = await fetch(`/api/progress?${p}`); if (!r.ok) throw new Error();
      const data = await r.json(); if (seq !== loadSeq) return;
      total = data.total;
      const pageTotal = Math.max(1, Math.ceil(total / pageSize));
      if (page > pageTotal) { page = pageTotal; syncForm(); return load(false); }
      tbody.innerHTML = data.results.map(x => {
        const rank = states[x.state] ?? 0, live = x.state === 'published';
        const kinds = lang === 'ja' ? {add:'追加',remove:'取り下げ',correct:'訂正'} : {add:'Add',remove:'Remove',correct:'Correct'};
        const statuses = lang === 'ja' ? {pending:'審査待ち',approved:'承認',rejected:'却下'} : {pending:'Pending',approved:'Approved',rejected:'Rejected'};
        const mediaStates = lang === 'ja' ? {pending:'待機',failed:'再試行待ち',none:'なし',ok:'完了',excluded:'除外'} : {pending:'Queued',failed:'Retrying',none:'None',ok:'Ready',excluded:'Excluded'};
        const mediaCell = (state, present) => {
          if (!state && !live) return `<td class="">—</td>`;
          const value = state || (present ? 'ok' : 'pending');
          return `<td class="${value === 'ok' || value === 'none' || value === 'excluded' ? 'done' : 'wait'}">${esc(mediaStates[value] || '—')}</td>`;
        };
        const request = x.request_type ? `${kinds[x.request_type]}: ${statuses[x.request_status]}` : '—';
        return `<tr><td class="url"><a href="${esc(x.url)}" rel="nofollow noopener" target="_blank">${esc(x.url)}</a></td>`+
          `<td>${x.source === 'request' ? (lang === 'ja' ? 'ユーザー申請' : 'User request') : (lang === 'ja' ? '自動発見' : 'Discovered')}</td>`+
          `${done(rank >= 2)}${done(rank >= 5)}`+
          `<td class="desc ${x.description_ja || rank >= 5 ? 'done' : 'wait'}">${esc(x.description_ja || (rank >= 5 ? '✓' : '…'))}</td><td class="desc ${x.description_en || rank >= 5 ? 'done' : 'wait'}">${esc(x.description_en || (rank >= 5 ? '✓' : '…'))}</td>`+
          `${cell(x.tags_count > 0 || rank >= 5, x.tags_count ? `${x.tags_count}${lang === 'ja' ? '件' : ''}` : rank >= 5 ? '✓' : '…')}`+
          `${cell(x.categories_count > 0 || rank >= 5, x.categories_count ? `${x.categories_count}${lang === 'ja' ? '件' : ''}` : rank >= 5 ? '✓' : '…')}`+
          `<td class="${x.request_status === 'pending' ? 'wait' : x.request_status ? 'done' : ''}">${esc(request)}</td>`+
          `${mediaCell(x.favicon_state, !!x.favicon)}${mediaCell(x.thumbnail_state, !!x.thumbnail)}${cell(live, lang === 'ja' ? '掲載済み' : 'Live')}</tr>`;
      }).join('') || `<tr><td colspan="12">${lang === 'ja' ? '該当するURLはありません。' : 'No matching URLs.'}</td></tr>`;
      const start = total ? (page - 1) * pageSize + 1 : 0, end = Math.min(page * pageSize, total);
      document.querySelector('#summary').textContent = lang === 'ja' ? `${start.toLocaleString()}〜${end.toLocaleString()} 件 / ${total.toLocaleString()} 件` : `Showing ${start.toLocaleString()}–${end.toLocaleString()} of ${total.toLocaleString()}`;
      updatePager();
    } catch {
      if (seq === loadSeq) tbody.innerHTML = `<tr><td colspan="12">${lang === 'ja' ? '読み込めませんでした。再読み込みしてください。' : 'Could not load. Reload the page.'}</td></tr>`;
    }
  }
  form.addEventListener('submit', e => { e.preventDefault(); filters = {q:queryInput.value.trim(),source:sourceSelect.value,state:stateSelect.value,missing:missingSelect.value}; page=1; load(true); });
  document.querySelector('#clear-filters').addEventListener('click', () => { filters = {q:'',source:'all',state:'all',missing:'all'}; page=1; syncForm(); load(true); });
  sizes.forEach(s => s.addEventListener('change', () => { pageSize=Number(s.value); page=1; syncForm(); load(true); }));
  pageButtons.forEach(b => b.addEventListener('click', () => {
    const last = Math.max(1, Math.ceil(total / pageSize)), action = b.dataset.pageAction;
    if (action === 'first') page=1; else if (action === 'prev') page=Math.max(1,page-1); else if (action === 'next') page=Math.min(last,page+1); else if (action === 'last') page=last;
    else { const wanted=Number(b.closest('.pager')?.querySelector('.page-input')?.value); if (!Number.isFinite(wanted)) return; page=Math.max(1,Math.min(last,Math.trunc(wanted))); }
    syncForm(); load(true);
  }));
  pageInputs.forEach(i => i.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); const wanted=Number(i.value), last=Math.max(1,Math.ceil(total/pageSize)); if (Number.isFinite(wanted)) { page=Math.max(1,Math.min(last,Math.trunc(wanted))); syncForm(); load(true); } } }));
  window.addEventListener('popstate', () => { readUrl(); load(false); });
  readUrl(); load(false);
})();
