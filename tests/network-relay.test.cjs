const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const script = readFileSync(join(__dirname, '../assets/network-relay.js'), 'utf8');
const slug = 'china-linked-precursor-incidents-official-record';
const article = () => ({ id: 'narcoscope.newsroom.' + slug, slug,
  dossierUrl: '/news/' + slug + '.dossier.json', htmlUrl: '/news/' + slug + '.html' });
const dossier = () => ({ schemaVersion: 'narcoscope.newsroom.evidence-analysis.v1', slug,
  title: 'Updated official evidence', dek: 'Updated scope and limits', dataAsOf: '2026-08-13',
  keyFigures: [{ id: 'china-eu-incident-count', value: 10, unit: 'incidents' },
    { id: 'china-eu-upper-bound-mass', value: 6, unit: 'tonnes, nearly' }],
  verificationReceipt: { visualCitationCoverage: { percent: 90 } } });

async function run({ articles = [article()], payload = dossier(), reject = false } = {}) {
  const elements = Object.fromEntries(['title', 'dek', 'feed-state', 'story-link', 'data-as-of',
    'incidents', 'tonnes', 'citations'].map(key => ['[data-ns-' + key + ']', {
      textContent: 'original ' + key, href: 'original link', dateTime: '2026-08-12',
      getAttribute(name) { return name === 'datetime' ? this.dateTime : null; },
    }]));
  const before = JSON.stringify(elements);
  const requests = [];
  let state = 'dated-fallback';
  const root = { querySelector: selector => elements[selector],
    setAttribute: (name, value) => { if (name === 'data-relay-state') state = value; } };
  const fetch = async url => {
    requests.push(url);
    if (reject) throw new Error('offline');
    return { ok: true, json: async () => requests.length === 1
      ? { schemaVersion: 'narcoscope.newsroom.index.v1', articles } : payload };
  };
  vm.runInNewContext(script, { document: { querySelector: () => root },
    window: { fetch, setTimeout: () => 1, clearTimeout() {} }, fetch, URL, Intl, Date });
  for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
  return { elements, before, requests, state };
}

test('a complete matching dossier updates the whole card from the canonical origin', async () => {
  const out = await run();
  assert.equal(out.state, 'remote');
  assert.equal(out.elements['[data-ns-title]'].textContent, 'Updated official evidence');
  assert.equal(out.elements['[data-ns-incidents]'].textContent, '10');
  assert.equal(out.elements['[data-ns-data-as-of]'].dateTime, '2026-08-13');
  assert.ok(out.requests.every(url => url.startsWith('https://www.narcoscope.com/')));
});

test('new unrelated newsroom entries cannot replace this investigation', async () => {
  const out = await run({ articles: [{ ...article(), id: 'unrelated', slug: 'other' }, article()] });
  assert.equal(out.state, 'remote');
  assert.match(out.requests[1], new RegExp(slug));
  const absent = await run({ articles: [{ ...article(), id: 'unrelated', slug: 'other' }] });
  assert.equal(absent.state, 'dated-fallback');
  assert.equal(absent.requests.length, 1);
});

for (const [name, mutate] of Object.entries({
  'wrong schema': d => { d.schemaVersion = 'unrelated'; },
  'wrong story': d => { d.slug = 'another-story'; },
  'blank title': d => { d.title = ' '; },
  'missing figures': d => { d.keyFigures = []; },
  'negative count': d => { d.keyFigures[0].value = -1; },
  'fractional count': d => { d.keyFigures[0].value = 1.5; },
  'wrong unit': d => { d.keyFigures[1].unit = 'kilograms'; },
  'invalid coverage': d => { d.verificationReceipt.visualCitationCoverage.percent = 101; },
  'impossible date': d => { d.dataAsOf = '2026-02-30'; },
  'older date': d => { d.dataAsOf = '2026-08-11'; },
  'future date': d => { d.dataAsOf = '9999-12-31'; },
})) {
  test(name + ' retains every original fallback field', async () => {
    const payload = dossier(); mutate(payload);
    const out = await run({ payload });
    assert.equal(out.state, 'dated-fallback');
    assert.equal(JSON.stringify(out.elements), out.before);
  });
}

test('foreign, credential-bearing and unrelated dossier URLs are rejected before fetching', async () => {
  for (const url of ['https://example.org/x', 'https://user' + '@' + 'www.narcoscope.com/news/' + slug + '.dossier.json', '/news/other.dossier.json']) {
    const out = await run({ articles: [{ ...article(), dossierUrl: url }] });
    assert.equal(out.state, 'dated-fallback');
    assert.equal(out.requests.length, 1);
  }
});

test('a fetch failure retains the complete fallback', async () => {
  const out = await run({ reject: true });
  assert.equal(out.state, 'dated-fallback');
  assert.equal(JSON.stringify(out.elements), out.before);
});
