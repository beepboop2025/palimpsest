/* Progressive enhancement: the complete source record remains readable without JS. */
(() => {
  const family = document.getElementById('ed-family');
  const search = document.getElementById('ed-search');
  const count = document.getElementById('ed-result-count');
  const releases = [...document.querySelectorAll('.ed-release')];
  if (!family || !search || !count) return;
  const searchable = new Map(releases.map(node => [node, node.textContent.toLocaleLowerCase()]));
  function filter() {
    const query = search.value.trim().toLocaleLowerCase();
    let visible = 0;
    for (const node of releases) {
      const selected = family.value === 'all' || family.value === node.dataset.family;
      node.hidden = !selected || !searchable.get(node).includes(query);
      if (!node.hidden) visible += 1;
      for (const detail of node.querySelectorAll('details')) {
        const matches = !query || detail.textContent.toLocaleLowerCase().includes(query);
        detail.hidden = Boolean(query) && !matches;
        if (query && matches && !node.hidden) detail.open = true;
      }
    }
    count.textContent = `${visible} economic areas shown`;
  }
  family.addEventListener('change', filter);
  search.addEventListener('input', filter);
  filter();
})();
