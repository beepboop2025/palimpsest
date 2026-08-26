'use strict';

// Server responses are bounded and normalized, but source-derived labels remain
// untrusted and are escaped again at the DOM boundary.
function esc(value) {
  return String(value == null ? '' : value).replace(
    /[&<>"']/g,
    (character) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]),
  );
}

function num(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function fmtLat(seconds) {
  if (seconds == null) return '–';
  const value = num(seconds);
  if (value < 3600) return `${Math.round(value / 60)}m`;
  if (value < 86400) return `${(value / 3600).toFixed(1)}h`;
  return `${(value / 86400).toFixed(1)}d`;
}

async function fetchJson(path) {
  const response = await fetch(path, {cache: 'no-store', credentials: 'omit'});
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    throw new Error('invalid JSON response');
  }
  if (!response.ok) {
    const error = new Error('measurement unavailable');
    error.payload = payload;
    throw error;
  }
  return payload;
}

function unavailableMessage(payload, fallback) {
  if (payload && typeof payload.reason === 'string' && payload.reason) {
    return payload.reason;
  }
  return fallback;
}

async function load() {
  let velocity = null;
  let velocityError = null;
  let deletions = null;
  let deletionError = null;

  try {
    velocity = await fetchJson('velocity');
  } catch (error) {
    velocityError = error && error.payload ? error.payload : null;
  }
  try {
    deletions = await fetchJson('deletions?limit=50');
  } catch (error) {
    deletionError = error && error.payload ? error.payload : null;
  }

  const measured = Boolean(velocity && velocity.status === 'ok');
  const ranked = measured && Array.isArray(velocity.ranked) ? velocity.ranked : [];
  const spikes = ranked.filter((row) => row && row.spike === true).length;
  document.getElementById('c-del').textContent = measured ? num(velocity.n_deletions) : '–';
  document.getElementById('c-terms').textContent = measured ? num(velocity.n_terms) : '–';
  document.getElementById('c-spikes').textContent = measured ? spikes : '–';
  document.getElementById('c-top').innerHTML = measured && velocity.top_term
    ? esc(velocity.top_term)
    : '<span class="quiet">–</span>';

  if (measured) {
    document.getElementById('stamp').textContent = `updated ${velocity.generated_at}`;
  } else {
    const state = velocity && typeof velocity.status === 'string'
      ? velocity.status
      : (velocityError && velocityError.status) || 'unavailable';
    const reason = unavailableMessage(
      velocity || velocityError,
      'No current velocity measurement is available.',
    );
    document.getElementById('stamp').textContent = `${state}: ${reason}`;
  }

  if (!measured) {
    const reason = unavailableMessage(
      velocity || velocityError,
      'No current velocity measurement is available.',
    );
    document.getElementById('rank-body').innerHTML =
      `<tr><td colspan="5" class="empty">Measurement unavailable: ${esc(reason)}</td></tr>`;
  } else if (ranked.length) {
    const maxZ = Math.max(1, ...ranked.map((row) => num(row.z)));
    document.getElementById('rank-body').innerHTML = ranked.map((row) => {
      const width = Math.max(0, Math.min(100, Math.round(num(row.z) / maxZ * 100)));
      const status = row.spike
        ? '<span class="spike">SPIKE</span>'
        : '<span class="quiet">—</span>';
      const domain = row.domain ? ` <span class="dom">${esc(row.domain)}</span>` : '';
      return `<tr><td><span class="term">${esc(row.term)}</span>${domain}`
        + `<meter class="bar" min="0" max="100" value="${width}"></meter></td>`
        + `<td>${num(row.count)}</td><td>${num(row.velocity_per_hour)}</td>`
        + `<td>${num(row.z).toFixed(2)}</td><td>${status}</td></tr>`;
    }).join('');
  } else {
    document.getElementById('rank-body').innerHTML =
      '<tr><td colspan="5" class="empty">Measured: no deletions in the current window.</td></tr>';
  }

  if (Array.isArray(deletions) && deletions.length) {
    document.getElementById('del-body').innerHTML = deletions.map((row) => {
      const keywords = (Array.isArray(row.keywords) ? row.keywords : [])
        .map((keyword) => `<span class="chip">${esc(keyword)}</span>`)
        .join('');
      return `<tr><td>${esc(row.source)}</td><td class="quiet">${esc(row.post_id)}</td>`
        + `<td>${esc((row.deleted_at || '').replace('T', ' ').slice(0, 19))}</td>`
        + `<td class="lat">${fmtLat(row.latency_seconds)}</td>`
        + `<td>${keywords || '<span class="quiet">—</span>'}</td></tr>`;
    }).join('');
  } else if (Array.isArray(deletions)) {
    document.getElementById('del-body').innerHTML =
      '<tr><td colspan="5" class="empty">Measured: no confirmed deletions recorded.</td></tr>';
  } else {
    const reason = unavailableMessage(
      deletionError,
      'The confirmed-deletion history is unavailable.',
    );
    document.getElementById('del-body').innerHTML =
      `<tr><td colspan="5" class="empty">History unavailable: ${esc(reason)}</td></tr>`;
  }
}

load();
setInterval(load, 30000);
