import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = readFileSync(new URL('../dashboards/ddti_dashboard.html', import.meta.url), 'utf8');
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).find(script => script.includes('async function boot()'));
assert.ok(source);

function index(at = new Date().toISOString()) {
  return { generated_at: at, n_terms: 1, n_observations: 7,
    window: { current_days: 45, history_days: 180 }, ranked: [{
      term: '<img src=x onerror=alert(1)>', threat: 1.2, attention: 0.8,
      novelty: 0.5, recent_count: 2, hist_count: 1, is_new: false,
      samples: [{ title: '<script>bad()</script>' }],
    }] };
}

async function dashboard(payload, embedded) {
  const elements = new Map();
  const requests = [];
  const intervals = [];
  let response = payload;
  const context = vm.createContext({
    window: { __DDTI_EMBED__: embedded }, Date, AbortSignal,
    document: { hidden: false, getElementById(id) {
      if (!elements.has(id)) elements.set(id, { textContent: '', innerHTML: '', className: '' });
      return elements.get(id);
    } },
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (response instanceof Error) throw response;
      return { ok: true, json: async () => response };
    },
    setInterval: (fn, ms) => intervals.push({ fn, ms }),
  });
  vm.runInContext(source, context);
  await new Promise(setImmediate);
  return { elements, requests, intervals, context,
    async refresh(next) { response = next; await vm.runInContext('boot()', context); },
  };
}

test('loads the published index, escapes source text, and refreshes only while visible', async () => {
  const d = await dashboard(index());
  assert.equal(d.requests[0].url, '/readings/ddti-latest.json');
  assert.equal(d.requests[0].options.cache, 'no-store');
  assert.equal(d.elements.get('feedTag').textContent, 'PUBLISHED SNAPSHOT');
  assert.match(d.elements.get('stats').innerHTML, /Source observations/);
  assert.match(d.elements.get('board').innerHTML, /&lt;img/);
  assert.doesNotMatch(d.elements.get('board').innerHTML, /<script>|<img/);
  const refresh = d.intervals.find(x => x.ms === 60000);
  assert.ok(refresh);
  d.context.document.hidden = true;
  refresh.fn();
  assert.equal(d.requests.length, 1);
});

test('retains the last dated record when a refresh fails or returns an older edition', async () => {
  const value = index();
  const d = await dashboard(value);
  const before = d.elements.get('board').innerHTML;
  await d.refresh(new Error('offline'));
  assert.equal(d.elements.get('feedTag').textContent, 'LAST SNAPSHOT');
  assert.match(d.elements.get('feedNote').textContent, /Refresh delayed/);
  assert.ok(d.elements.get('feedNote').textContent.includes(value.generated_at));
  assert.equal(d.elements.get('board').innerHTML, before);
  await d.refresh(index(new Date(Date.now() - 3600000).toISOString()));
  assert.equal(d.elements.get('feedTag').textContent, 'LAST SNAPSHOT');
  assert.equal(d.elements.get('board').innerHTML, before);
});

test('labels old embedded evidence as dated instead of live', async () => {
  const d = await dashboard(new Error('offline'), index(new Date(Date.now() - 4 * 3600000).toISOString()));
  assert.equal(d.elements.get('feedTag').textContent, 'DATED SNAPSHOT');
  assert.doesNotMatch(d.elements.get('feedTag').textContent, /LIVE/);
});

test('never substitutes invented rankings for missing, malformed, or future evidence', async () => {
  const malformed = index();
  malformed.ranked[0].threat = 'not a measurement';
  const missingWindow = index();
  delete missingWindow.window;
  const missingCount = index();
  delete missingCount.n_observations;
  const malformedFlag = index();
  malformedFlag.ranked[0].is_new = 'false';
  for (const payload of [new Error('offline'), { status: 'unavailable' }, malformed,
    missingWindow, missingCount, malformedFlag, { ...index(), ranked: [] },
    index(new Date(Date.now() + 3600000).toISOString())]) {
    const d = await dashboard(payload);
    assert.equal(d.elements.get('feedTag').textContent, 'DATA UNAVAILABLE');
    assert.equal(d.elements.get('board').textContent, 'No published ranking is available.');
    assert.equal(d.elements.get('board').innerHTML, '');
  }
  assert.doesNotMatch(source, /const SAMPLE|\/api\/v4\/ddti/);
});
