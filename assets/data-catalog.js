/* Progressive enhancement for the Evidence Atlas. No search query leaves the browser. */
(function () {
  "use strict";

  var catalog = null;
  var list = document.getElementById("dataset-list");
  var loom = document.getElementById("loom");
  var form = document.getElementById("catalog-filters");
  var search = document.getElementById("catalog-search");
  var layer = document.getElementById("filter-layer");
  var mode = document.getElementById("filter-mode");
  var state = document.getElementById("filter-state");
  var count = document.getElementById("result-count");

  function text(value) {
    return value === null || value === undefined ? "" : String(value);
  }

  function element(name, className, content) {
    var node = document.createElement(name);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = text(content);
    return node;
  }

  function formatBytes(value) {
    var bytes = Number(value) || 0;
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    var i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i += 1; }
    return (i ? bytes.toFixed(bytes >= 10 ? 1 : 2) : String(Math.round(bytes))) + " " + units[i];
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("en", { notation: Number(value) >= 1000000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(Number(value) || 0);
  }

  function human(value) {
    return text(value).replaceAll("-", " ").replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  function relativeTime(iso) {
    if (!iso) return "No evidence timestamp";
    var delta = Date.now() - Date.parse(iso);
    if (!Number.isFinite(delta)) return iso;
    var minutes = Math.max(0, Math.floor(delta / 60000));
    if (minutes < 90) return minutes + " min ago";
    var hours = Math.floor(minutes / 60);
    if (hours < 48) return hours + " h ago";
    return Math.floor(hours / 24) + " d ago";
  }

  function optionValues(select, values) {
    Array.from(new Set(values)).sort().forEach(function (value) {
      var opt = element("option", "", human(value));
      opt.value = value;
      select.appendChild(opt);
    });
  }

  function stageIndex(stage) {
    return ({ observation: 0, corroboration: 1, synthesis: 2, quality: 3, integrity: 4, archive: 4 })[stage] ?? 0;
  }

  function renderLoom(items) {
    var loading = loom.querySelector(".loom__loading");
    if (loading) loading.remove();
    items.forEach(function (item, index) {
      var row = element("div", "loom__row");
      row.dataset.id = item.id;
      row.dataset.layer = item.layer;
      row.dataset.search = searchable(item);
      row.style.setProperty("--i", index);
      var name = element("a", "loom__name", item.name);
      name.href = "#dataset-" + item.id;
      name.appendChild(element("small", "", human(item.layer) + " · " + human(item.collection_mode)));
      row.appendChild(name);
      var target = stageIndex(item.stage);
      for (var i = 0; i < 5; i += 1) {
        var cell = element("span", "loom__cell");
        if (i <= target) cell.classList.add("is-path");
        if (i === target) cell.classList.add("is-node");
        row.appendChild(cell);
      }
      loom.appendChild(row);
    });
  }

  function metaRow(term, value, link) {
    var box = element("div");
    box.appendChild(element("dt", "", term));
    var dd = element("dd");
    if (link) {
      var a = element("a", "", value);
      a.href = link;
      dd.appendChild(a);
    } else dd.textContent = value;
    box.appendChild(dd);
    return box;
  }

  function fileLink(label, url, available) {
    if (available === false) {
      return element("span", "dataset__file-pending", label + " · not public");
    }
    var a = element("a", "", label);
    a.href = url;
    return a;
  }

  function searchable(item) {
    return [item.id, item.name, item.description, item.layer, item.stage, item.collection_mode,
      item.status, item.artifacts.evidence_state].concat(item.geography, item.sources).join(" ").toLowerCase();
  }

  function renderDataset(item, index) {
    var details = element("details", "dataset");
    details.id = "dataset-" + item.id;
    details.dataset.id = item.id;
    details.dataset.layer = item.layer;
    details.dataset.mode = item.collection_mode;
    details.dataset.state = item.artifacts.evidence_state;
    details.dataset.search = searchable(item);

    var summary = element("summary");
    summary.appendChild(element("span", "dataset__index", String(index + 1).padStart(2, "0")));
    var title = element("span", "dataset__title");
    title.appendChild(element("b", "", item.name));
    title.appendChild(element("code", "", item.id));
    summary.appendChild(title);

    [["Layer", item.layer, "dataset__facet--layer"], ["Source", item.sources.join(", "), "dataset__facet--source"],
      ["Collection", item.collection_mode, "dataset__facet--mode"]].forEach(function (entry) {
      var facet = element("span", "dataset__facet " + entry[2]);
      facet.appendChild(element("span", "", entry[0]));
      facet.appendChild(element("b", "", human(entry[1])));
      summary.appendChild(facet);
    });
    var evidence = element("span", "dataset__facet dataset__state");
    evidence.textContent = human(item.artifacts.evidence_state);
    evidence.title = item.artifacts.observed_at ? (item.artifacts.observed_at + " · " + relativeTime(item.artifacts.observed_at)) : "No public evidence timestamp";
    summary.appendChild(evidence);
    details.appendChild(summary);

    var body = element("div", "dataset__body");
    var main = element("div");
    main.appendChild(element("p", "dataset__description", item.description));
    var tags = element("div", "dataset__tags");
    [item.stage, item.cadence].concat(item.geography).forEach(function (value) { tags.appendChild(element("span", "dataset__tag", value)); });
    main.appendChild(tags);
    var files = element("div", "dataset__files");
    files.appendChild(fileLink("Current JSON", item.urls.latest, item.artifacts.latest_available));
    if (item.urls.history) files.appendChild(fileLink("History JSONL", item.urls.history, item.artifacts.history_available));
    files.appendChild(fileLink("Read the method", item.urls.method, true));
    files.appendChild(fileLink("Open landing page", item.urls.landing_page, true));
    main.appendChild(files);
    body.appendChild(main);

    var meta = element("dl", "dataset__meta");
    meta.appendChild(metaRow("Observed", item.artifacts.observed_at ? relativeTime(item.artifacts.observed_at) : "Not published"));
    meta.appendChild(metaRow("Collection target", item.cadence));
    meta.appendChild(metaRow("History", formatNumber(item.artifacts.history_rows) + " rows · " + formatBytes(item.artifacts.history_bytes)));
    var counts = Object.entries(item.artifacts.counts || {}).map(function (pair) { return pair[0] + "=" + formatNumber(pair[1]); }).join(" · ");
    meta.appendChild(metaRow("Counts", counts || "No single headline denominator"));
    meta.appendChild(metaRow("Sources", item.sources.join(" · ")));
    meta.appendChild(metaRow("Rights", item.license.name, item.license.url));
    body.appendChild(meta);
    details.appendChild(body);
    return details;
  }

  function matches(node) {
    var q = search.value.trim().toLowerCase();
    return (!q || node.dataset.search.indexOf(q) !== -1) &&
      (!layer.value || node.dataset.layer === layer.value) &&
      (!mode.value || node.dataset.mode === mode.value) &&
      (!state.value || node.dataset.state === state.value);
  }

  function applyFilters() {
    var visible = 0;
    list.querySelectorAll(".dataset").forEach(function (node) {
      node.hidden = !matches(node);
      if (!node.hidden) visible += 1;
    });
    loom.querySelectorAll(".loom__row").forEach(function (node) {
      var q = search.value.trim().toLowerCase();
      node.hidden = (q && node.dataset.search.indexOf(q) === -1) || (layer.value && node.dataset.layer !== layer.value);
    });
    count.textContent = String(visible);
    var prior = list.querySelector(".atlas-empty--filter");
    if (prior) prior.remove();
    if (!visible) {
      var empty = element("div", "atlas-empty atlas-empty--filter", "No dataset matches those filters. Clear one filter or search for a source name.");
      list.appendChild(empty);
    }
  }

  function render(data) {
    catalog = data;
    document.getElementById("stat-datasets").textContent = formatNumber(data.summary.datasets);
    document.getElementById("stat-fresh").textContent = formatNumber(data.summary.states.fresh || 0);
    document.getElementById("stat-rows").textContent = formatNumber(data.summary.history_rows);
    document.getElementById("stat-bytes").textContent = formatBytes(data.summary.published_bytes);
    document.getElementById("catalog-asof").textContent = "Catalog built " + relativeTime(data.generated_at) + ". Freshness is computed from evidence timestamps, never file modification time.";

    optionValues(layer, data.datasets.map(function (d) { return d.layer; }));
    optionValues(mode, data.datasets.map(function (d) { return d.collection_mode; }));
    optionValues(state, data.datasets.map(function (d) { return d.artifacts.evidence_state; }));
    list.textContent = "";
    data.datasets.forEach(function (item, index) { list.appendChild(renderDataset(item, index)); });
    renderLoom(data.datasets);
    count.textContent = String(data.datasets.length);
  }

  function fail() {
    list.textContent = "";
    var box = element("div", "atlas-empty");
    box.append("The catalog could not be loaded. The files are still available in the ");
    var a = element("a", "", "plain readings directory");
    a.href = "/readings/";
    box.appendChild(a);
    box.append(".");
    list.appendChild(box);
    document.getElementById("catalog-asof").textContent = "Catalog unavailable; no freshness claim is shown.";
  }

  if (form) {
    form.addEventListener("input", applyFilters);
    form.addEventListener("reset", function () { window.setTimeout(applyFilters, 0); });
  }
  fetch("/readings/catalog.json", { cache: "no-cache", credentials: "omit" })
    .then(function (response) { if (!response.ok) throw new Error("catalog " + response.status); return response.json(); })
    .then(render)
    .catch(fail);
}());
