// The counter is optional: a network failure must not affect the directory.
(async () => {
  try {
    const response = await fetch('https://api.nostalgic.llll-ll.com/visit?action=increment&id=mise-31f9ea08&format=text', { cache: 'no-store' });
    if (!response.ok) return;
    const count = (await response.text()).trim();
    if (!/^\d+$/.test(count)) return;
    document.getElementById('visit-count').textContent = Number(count).toLocaleString();
    document.getElementById('visit-counter').hidden = false;
  } catch (_) { /* no counter while offline */ }
})();

// Build date is generated once by Wrangler, in JST, and does not change per request.
fetch('/build-date.json', { cache: 'no-store' }).then(response => {
  if (!response.ok) throw new Error('Build date unavailable');
  return response.json();
}).then(({ date }) => {
  if (/^\d{4}-\d{2}-\d{2}$/.test(date)) document.getElementById('build-version').textContent = `v${date}`;
}).catch(() => {});
