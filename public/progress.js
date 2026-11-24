(() => {
  let savedLanguage = null; try { savedLanguage = localStorage.getItem('mise-language'); } catch {}
  const pageSize = 50, tbody = document.querySelector('#rows'), lang = (savedLanguage || navigator.language || 'en').startsWith('ja') ? 'ja' : 'en';
  document.documentElement.lang = lang;
  document.querySelectorAll('[data-ja][data-en]').forEach(e => { e.textContent = e.dataset[lang]; });
  let offset = 0, total = 0;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const cell = (ok, yes, no='…') => `<td class="${ok ? 'done' : 'wait'}">${ok ? yes : no}</td>`;
  const states = {discovered:0,queued:1,fetched:2,candidate:3,reviewing:4,approved:5,rejected:5,failed:2,blocked:2,published:8};
  const done = (ok, dash=false) => cell(ok, '✓', dash ? '—' : '…');
  async function load() {
    tbody.innerHTML = `<tr><td colspan="12">${lang === 'ja' ? '読み込み中…' : 'Loading…'}</td></tr>`;
    try {
      const r = await fetch(`/api/progress?limit=${pageSize}&offset=${offset}`); if (!r.ok) throw new Error();
      const data = await r.json(); total = data.total;
      tbody.innerHTML = data.results.map(x => {
        const rank = states[x.state] ?? 0, live = x.state === 'published';
        const kinds = lang === 'ja' ? {add:'追加',remove:'取り下げ',correct:'訂正'} : {add:'Add',remove:'Remove',correct:'Correct'};
        const statuses = lang === 'ja' ? {pending:'審査待ち',approved:'承認',rejected:'却下'} : {pending:'Pending',approved:'Approved',rejected:'Rejected'};
        const request = x.request_type ? `${kinds[x.request_type]}: ${statuses[x.request_status]}` : '—';
        return `<tr><td class="url"><a href="${esc(x.url)}" rel="nofollow noopener" target="_blank">${esc(x.url)}</a></td>`+
          `<td>${x.source === 'request' ? (lang === 'ja' ? 'ユーザー申請' : 'User request') : (lang === 'ja' ? '自動発見' : 'Discovered')}</td>`+
          `${done(rank >= 2)}${done(rank >= 5)}`+
          `<td class="desc ${x.description_ja || rank >= 5 ? 'done' : 'wait'}">${esc(x.description_ja || (rank >= 5 ? '✓' : '…'))}</td><td class="desc ${x.description_en || rank >= 5 ? 'done' : 'wait'}">${esc(x.description_en || (rank >= 5 ? '✓' : '…'))}</td>`+
          `${cell(x.tags_count > 0 || rank >= 5, x.tags_count ? `${x.tags_count}${lang === 'ja' ? '件' : ''}` : rank >= 5 ? '✓' : '…')}`+
          `${cell(x.categories_count > 0 || rank >= 5, x.categories_count ? `${x.categories_count}${lang === 'ja' ? '件' : ''}` : rank >= 5 ? '✓' : '…')}`+
          `<td class="${x.request_status === 'pending' ? 'wait' : x.request_status ? 'done' : ''}">${esc(request)}</td>`+
          `${done(!!x.favicon)}${done(!!x.thumbnail)}${cell(live, lang === 'ja' ? '掲載済み' : 'Live')}</tr>`;
      }).join('') || `<tr><td colspan="12">${lang === 'ja' ? '表示する項目はありません。' : 'No entries.'}</td></tr>`;
      document.querySelector('#summary').textContent = lang === 'ja' ? `${total.toLocaleString()} 件` : `${total.toLocaleString()} URLs`;
      document.querySelector('#page').textContent = `${Math.floor(offset / pageSize) + 1} / ${Math.max(1, Math.ceil(total / pageSize))}`;
      document.querySelector('#prev').disabled = offset === 0; document.querySelector('#next').disabled = offset + pageSize >= total;
    } catch { tbody.innerHTML = `<tr><td colspan="12">${lang === 'ja' ? '読み込めませんでした。再読み込みしてください。' : 'Could not load. Reload the page.'}</td></tr>`; }
  }
  document.querySelector('#prev').addEventListener('click', () => { offset = Math.max(0, offset - pageSize); load(); });
  document.querySelector('#next').addEventListener('click', () => { offset += pageSize; load(); }); load();
})();
