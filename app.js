(async function () {
  const [odds, transfers, plTransfers, rumors, sources] = await Promise.all([
    fetchJSON('data/odds.json'),
    fetchJSON('data/transfers.json'),
    fetchJSON('data/pl-transfers.json'),
    fetchJSON('data/rumors.json'),
    fetchJSON('data/sources.json'),
  ]);

  renderOdds(odds);
  renderTransfers(transfers);
  renderPLTransfers(plTransfers);
  renderRumors(rumors);
  renderSources(sources);
  renderLastUpdated([odds, transfers, plTransfers, rumors]);
})();

async function fetchJSON(path) {
  try {
    const res = await fetch(path, { cache: 'no-store' });
    if (!res.ok) throw new Error(res.status);
    return await res.json();
  } catch (e) {
    console.error('Failed to load', path, e);
    return null;
  }
}

function renderOdds(data) {
  const grid = document.getElementById('odds-grid');
  if (!data || !data.items || !data.items.length) {
    grid.innerHTML = '<p class="empty">No odds yet.</p>';
    return;
  }
  grid.innerHTML = data.items.map(item => {
    const oddsDisplay = item.odds == null ? '—' : String(item.odds);
    const prob = (item.impliedProbability != null)
      ? ` <span class="prob">(${(item.impliedProbability * 100).toFixed(1)}%)</span>` : '';
    const book = item.bestBookmaker
      ? `<div class="book">${escapeHTML(item.bestBookmaker)}</div>` : '';
    return `
      <div class="odds-item">
        <div class="comp">${escapeHTML(item.competition)}</div>
        <div class="value">${escapeHTML(oddsDisplay)}${prob}</div>
        ${book}
        <div class="updated">Updated ${escapeHTML(item.lastUpdated || '')}</div>
      </div>
    `;
  }).join('');
}

function renderTransfers(data) {
  const listIn = document.getElementById('transfers-in');
  const listOut = document.getElementById('transfers-out');
  const window = data && data.window ? ` (${escapeHTML(data.window)})` : '';
  document.querySelector('#transfers-section h2').textContent = 'Summer Transfers' + window;

  fillTransferList(listIn, (data && data.in) || []);
  fillTransferList(listOut, (data && data.out) || []);
}

function fillTransferList(el, arr) {
  if (!arr.length) { el.innerHTML = '<li class="empty">None yet.</li>'; return; }
  el.innerHTML = arr.map(t => `
    <li>
      <span class="player">${escapeHTML(t.player)}</span>
      <span class="meta">
        ${escapeHTML(t.club || '')}${t.fee ? ' · ' + escapeHTML(t.fee) : ''}${t.date ? ' · ' + escapeHTML(t.date) : ''}
      </span>
    </li>
  `).join('');
}

function renderPLTransfers(data) {
  const list = document.getElementById('pl-transfers-list');
  const items = (data && data.items) || [];
  if (!items.length) { list.innerHTML = '<li class="empty">Nothing tracked yet.</li>'; return; }
  list.innerHTML = items.map(t => `
    <li>
      <span class="player">${escapeHTML(t.player)}</span>
      <span class="meta">
        ${escapeHTML(t.from || '')} → ${escapeHTML(t.to || '')}${t.fee ? ' · ' + escapeHTML(t.fee) : ''}${t.date ? ' · ' + escapeHTML(t.date) : ''}
      </span>
    </li>
  `).join('');
}

function renderRumors(data) {
  const list = document.getElementById('rumors-list');
  const items = (data && data.items) || [];
  if (!items.length) { list.innerHTML = '<li class="empty">No active rumors tracked.</li>'; return; }
  list.innerHTML = items.map(r => `
    <li>
      <span class="headline">${escapeHTML(r.headline)}</span>
      <span class="reliability ${escapeAttr(r.reliability || 'medium')}">${escapeHTML(r.reliability || 'medium')}</span>
      <span class="meta">
        Source: ${r.sourceUrl
          ? `<a href="${escapeAttr(r.sourceUrl)}" target="_blank" rel="noopener">${escapeHTML(r.source || r.sourceUrl)}</a>`
          : escapeHTML(r.source || 'unknown')}
        ${r.date ? ' · ' + escapeHTML(r.date) : ''}
      </span>
    </li>
  `).join('');
}

function renderSources(data) {
  const list = document.getElementById('sources-list');
  const items = (data && data.items) || [];
  if (!items.length) { list.innerHTML = '<li class="empty">No sources listed.</li>'; return; }
  list.innerHTML = items.map(s => `
    <li>
      <a href="${escapeAttr(s.url)}" target="_blank" rel="noopener">${escapeHTML(s.name)}</a>
      ${s.description ? `<span class="desc">${escapeHTML(s.description)}</span>` : ''}
    </li>
  `).join('');
}

function renderLastUpdated(datasets) {
  const dates = datasets
    .map(d => d && d.lastUpdated)
    .filter(Boolean)
    .sort();
  const latest = dates[dates.length - 1];
  if (latest) {
    document.getElementById('last-updated').textContent = 'Data last updated: ' + latest;
  }
}

function escapeHTML(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function escapeAttr(s) { return escapeHTML(s); }
