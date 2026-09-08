/* Public economic series retain periods, gaps, original units and source links. */
(() => {
  const city = document.getElementById('eo-city');
  city?.addEventListener('input', () => document.querySelectorAll('#eo-housing tbody tr').forEach(row => { row.hidden = !row.dataset.city.includes(city.value.trim().toLocaleLowerCase()); }));
  const select = document.getElementById('eo-series');
  const target = document.getElementById('eo-chart');
  if (!select || !target) return;
  const escape = text => String(text).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const month = period => Number(period.slice(0,4))*12 + Number(period.slice(5));
  let data;
  function draw() {
    const series = data.series.find(item => item.id === select.value);
    if (!series?.points.length) { target.textContent = 'No comparable observations for this measure.'; return; }
    const points = series.points;
    const values = points.map(point => point.value);
    const floor = Math.min(...values), ceiling = Math.max(...values), pad = Math.max((ceiling-floor)*.15,.5);
    const lo = floor-pad, hi = ceiling+pad;
    const start = month(points[0].period), end = month(points.at(-1).period);
    const x = point => 60+(month(point.period)-start)/Math.max(end-start,1)*820;
    const y = value => 240-(value-lo)/(hi-lo)*200;
    const line = points.map((point,index) => `${index && month(point.period) === month(points[index-1].period)+1 ? 'L' : 'M'}${x(point).toFixed(2)},${y(point.value).toFixed(2)}`).join(' ');
    const threshold = series.unit === 'diffusion index' && lo<=50 && hi>=50 ? `<line class="eo-threshold" x1="60" x2="880" y1="${y(50)}" y2="${y(50)}"/><text x="885" y="${y(50)+5}">50</text>` : '';
    const ticks = [lo,(lo+hi)/2,hi].map(value => `<line class="eo-grid" x1="60" x2="880" y1="${y(value)}" y2="${y(value)}"/><text x="5" y="${y(value)+5}">${value.toFixed(1)}</text>`).join('');
    const circles = points.map(point => `<circle cx="${x(point)}" cy="${y(point.value)}" r="3"><title>${escape(point.period)}: ${point.value}</title></circle>`).join('');
    const rows = points.slice().reverse().map(point => `<tr><th>${escape(point.period)}</th><td>${point.value}</td><td>${point.retained_source_vintages}</td><td><a href="${escape(point.source_url)}">${escape(point.released_at.slice(0,10))}</a></td></tr>`).join('');
    target.innerHTML = `<h3>${escape(series.label)}</h3><p>${escape(series.unit)} · ${escape(series.window)} · ${points.length} distinct months. Gaps break the line.</p><svg viewBox="0 0 940 285" role="img" aria-label="${escape(series.label)} from ${escape(points[0].period)} to ${escape(points.at(-1).period)}"><title>${escape(series.label)}</title>${ticks}${threshold}<path d="${line}"/>${circles}<text x="60" y="275">${escape(points[0].period)}</text><text x="825" y="275">${escape(points.at(-1).period)}</text></svg><details><summary>Read the chart as a table and inspect sources</summary><div class="eo-scroll"><table><thead><tr><th>Month</th><th>${escape(series.unit)}</th><th>Retained source vintages</th><th>Latest source release</th></tr></thead><tbody>${rows}</tbody></table></div></details>`;
  }
  select.addEventListener('change', () => { if (data) draw(); });
  fetch('/readings/china-economic-history-analysis-latest.json').then(response => { if (!response.ok) throw new Error('History unavailable'); return response.json(); }).then(value => {
    if (value.schema !== 'palimpsest.china-economic-history-analysis.v1' || !Array.isArray(value.series)) throw new Error('Unexpected history format');
    data = value;
    if (data.series.some(series => series.id === 'manufacturing:new-order-index')) select.value='manufacturing:new-order-index';
    draw();
  }).catch(() => { target.textContent='The chart could not load. Use the historical data download below to inspect the retained observations.'; });
})();
