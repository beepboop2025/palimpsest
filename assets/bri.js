(() => {
  const normalize = (value) => String(value || '').toLowerCase().trim()

  const filterTargets = () => {
    const input = document.querySelector('[data-bri-target-search]')
    const rows = [...document.querySelectorAll('[data-bri-record]')]
    if (!input || rows.length === 0) return
    const apply = () => {
      const query = normalize(input.value)
      rows.forEach((row) => { row.hidden = query !== '' && !normalize(row.dataset.briText).includes(query) })
    }
    input.addEventListener('input', apply)
    apply()
  }

  const filterSources = () => {
    const input = document.querySelector('[data-bri-source-search]')
    const select = document.querySelector('[data-bri-state]')
    const count = document.querySelector('[data-bri-source-count]')
    const rows = [...document.querySelectorAll('[data-bri-source]')]
    if (!input || !select || !count || rows.length === 0) return
    const apply = () => {
      const query = normalize(input.value)
      const state = select.value
      let visible = 0
      rows.forEach((row) => {
        const matchesText = query === '' || normalize(row.dataset.briText).includes(query)
        const matchesState = state === 'all' || row.dataset.state === state
        row.hidden = !(matchesText && matchesState)
        if (!row.hidden) visible += 1
      })
      count.textContent = `${visible} of ${rows.length} source families`
    }
    input.addEventListener('input', apply)
    select.addEventListener('change', apply)
    apply()
  }

  filterTargets()
  filterSources()
})()
