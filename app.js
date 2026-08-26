(async function () {
  const [odds, transfers, plTransfers, rumors, sources, tactics, glossary] = await Promise.all([
    fetchJSON('data/odds.json'),
    fetchJSON('data/transfers.json'),
    fetchJSON('data/pl-transfers.json'),
    fetchJSON('data/rumors.json'),
    fetchJSON('data/sources.json'),
    fetchJSON('data/tactics.json'),
    fetchJSON('data/glossary.json'),
  ]);

  renderTactics(tactics, glossary);
  renderGlossary(glossary, tactics);
  renderOdds(odds);
  renderTransfers(transfers);
  renderPLTransfers(plTransfers);
  renderRumors(rumors);
  renderSources(sources);
  renderLastUpdated([odds, transfers, plTransfers, rumors, tactics]);
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

function renderTactics(data, glossary) {
  const el = document.getElementById('tactics-body');
  const match = data && data.matches && data.matches[0];
  if (!match) {
    el.innerHTML = `<p class="empty">No match breakdown yet — the next digest run will add one.</p>`;
    return;
  }

  const g = match.grounded || {};
  const x = match.explained || {};
  const lessonNumber = ((data.conceptsTaught || []).length) || 1;
  const venue = g.homeAway === 'H' ? 'vs' : 'away at';

  const chips = [
    ['On the teamsheet', g.arsenalFormation],
    ['With the ball', x.shape && x.shape.arsenalInPossession],
    ['Without the ball', x.shape && x.shape.arsenalOutOfPossession],
  ].filter(([, v]) => v);

  const shapeGrid = chips.length ? `
    <div class="shape-grid">
      ${chips.map(([label, value]) => `
        <div class="shape-chip">
          <div class="shape-label">${escapeHTML(label)}</div>
          <div class="shape-value">${escapeHTML(value)}</div>
        </div>`).join('')}
    </div>
    ${g.opponentFormation
      ? `<p class="shape-opponent">${escapeHTML(g.opponent || 'Opponent')} lined up ${escapeHTML(g.opponentFormation)}</p>`
      : ''}` : '';

  const lesson = x.lesson || {};
  const lessonBlock = lesson.term ? `
    <div class="lesson-card">
      <div class="lesson-kicker">Lesson ${lessonNumber} · Level ${lesson.level || 1} of 3</div>
      <h3 class="lesson-term">${escapeHTML(lesson.term)}</h3>
      <p class="lesson-explain">${escapeHTML(lesson.explain || '')}</p>
      <div class="lesson-spot"><strong>Watch for it:</strong> ${escapeHTML(lesson.spotIt || '')}</div>
    </div>` : '';

  const stats = (x.statTranslations || []).map(t => `
    <tr>
      <th scope="row">${escapeHTML(t.stat || '')}</th>
      <td>${escapeHTML(t.plain || '')}</td>
    </tr>`).join('');

  el.innerHTML = `
    <div class="match-head">
      <span class="match-score">Arsenal ${escapeHTML(String(g.goalsFor ?? '?'))}–${escapeHTML(String(g.goalsAgainst ?? '?'))} ${escapeHTML(venue)} ${escapeHTML(g.opponent || '?')}</span>
      <span class="match-meta">${escapeHTML(g.competition || '')}${g.date ? ' · ' + escapeHTML(g.date) : ''}</span>
    </div>
    ${x.whatHappened ? `<p class="match-summary">${escapeHTML(x.whatHappened)}</p>` : ''}
    ${shapeGrid}
    ${x.shape && x.shape.plainEnglish ? callout('Why the shape changes', x.shape.plainEnglish, 'gold') : ''}
    ${x.opponentPlan ? callout('What they tried', x.opponentPlan) : ''}
    ${x.keyMoment ? callout('Turning point', x.keyMoment) : ''}
    ${lessonBlock}
    ${stats ? `<h4 class="mini-head">By the numbers</h4><table class="stat-table">${stats}</table>` : ''}
    ${x.nerdCorner ? `
      <div class="nerd-corner">
        <div class="nerd-kicker">Nerd corner</div>
        <p>${escapeHTML(x.nerdCorner)}</p>
      </div>` : ''}
    ${renderSourceLine(g.sources, match.confidence)}
  `;
}

function renderSourceLine(sources, confidence) {
  if (!sources || !sources.length) return '';
  const links = sources.map((url, i) =>
    `<a href="${escapeAttr(url)}" target="_blank" rel="noopener">[${i + 1}]</a>`
  ).join(' · ');
  const note = confidence ? ` · sourcing confidence: ${escapeHTML(confidence)}` : '';
  return `<p class="source-line">Sources: ${links}${note}</p>`;
}

function callout(label, text, tone) {
  return `
    <div class="callout${tone ? ' callout-' + tone : ''}">
      <div class="callout-label">${escapeHTML(label)}</div>
      <div>${escapeHTML(text)}</div>
    </div>`;
}

function renderGlossary(data, tactics) {
  const el = document.getElementById('glossary-list');
  const items = (data && data.items) || [];
  if (!items.length) { el.innerHTML = '<p class="empty">Glossary unavailable.</p>'; return; }

  const learned = new Set((tactics && tactics.conceptsTaught) || []);

  el.innerHTML = items.map(item => `
    <details class="concept" data-level="${escapeAttr(String(item.level))}">
      <summary>
        <span class="concept-term">${escapeHTML(item.term)}</span>
        <span class="concept-level level-${escapeAttr(String(item.level))}">L${escapeHTML(String(item.level))}</span>
        ${learned.has(item.id) ? '<span class="learned-chip">learned</span>' : ''}
        <span class="concept-short">${escapeHTML(item.short)}</span>
      </summary>
      <div class="concept-body">
        <p>${escapeHTML(item.plain)}</p>
        ${item.arsenal ? `<p class="concept-arsenal"><strong>At Arsenal:</strong> ${escapeHTML(item.arsenal)}</p>` : ''}
        ${item.spotIt ? `<p class="concept-spot"><strong>Spot it:</strong> ${escapeHTML(item.spotIt)}</p>` : ''}
      </div>
    </details>
  `).join('');

  document.querySelectorAll('.level-filter').forEach(btn => {
    btn.addEventListener('click', () => {
      const level = btn.dataset.level;
      document.querySelectorAll('.level-filter').forEach(b => b.classList.toggle('is-active', b === btn));
      document.querySelectorAll('.concept').forEach(c => {
        c.hidden = level !== 'all' && c.dataset.level !== level;
      });
    });
  });
}

function renderOdds(data) {
  const grid = document.getElementById('odds-grid');
  const multiplesGrid = document.getElementById('multiples-grid');
  if (!data || !data.items || !data.items.length) {
    grid.innerHTML = '<p class="empty">No odds yet.</p>';
    multiplesGrid.innerHTML = '';
    return;
  }
  // Parlays live in their own row so the trophies read as one set of five.
  const card = item => {
    // Kalshi prices are probabilities, so lead with the percentage and keep
    // decimal odds as the secondary read.
    const prob = item.impliedProbability == null
      ? '—' : `${(item.impliedProbability * 100).toFixed(1)}%`;
    const settled = item.source === 'settled';
    const derived = item.source === 'derived';
    // A settled trophy has no price to quote, and a parlay has no market of
    // its own — neither gets a decimal price or a spread.
    const dec = settled ? 'Won' : (item.odds == null ? '' : `${item.odds} decimal`);
    const spread = (!settled && item.bidCents != null && item.askCents != null)
      ? `<div class="spread">${item.bidCents}–${item.askCents}\u00a2 bid/ask</div>` : '';
    const legs = item.legs ? `<div class="legs">${escapeHTML(item.legs)}</div>` : '';
    const link = item.marketUrl
      ? `<a class="market-link" href="${escapeAttr(item.marketUrl)}" target="_blank" rel="noopener">Kalshi market →</a>`
      : '';
    const classes = ['odds-item'];
    if (settled) classes.push('is-settled');
    if (derived) classes.push('is-parlay');
    return `
      <div class="${classes.join(' ')}">
        <div class="comp">${escapeHTML(item.competition)}</div>
        ${legs}
        <div class="value">${escapeHTML(prob)}</div>
        <div class="book">${escapeHTML(dec)}</div>
        ${spread}
        ${link}
        <div class="updated">Updated ${escapeHTML(item.lastUpdated || '')}</div>
      </div>
    `;
  };

  const items = data.items;
  grid.innerHTML = items.filter(i => i.source !== 'derived').map(card).join('');
  multiplesGrid.innerHTML = items.filter(i => i.source === 'derived').map(card).join('');
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
