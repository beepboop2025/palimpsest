/* Reciprocal publication relay: NarcoScope -> Palimpsest.
   The HTML already contains a complete, dated fallback. This script may only
   replace text with a newer public NarcoScope dossier; it never manufactures a
   metric, injects markup, or turns a fetch failure into a live-looking state. */
(function () {
  "use strict";

  var root = document.querySelector("[data-narcoscope-relay]");
  if (!root || !("fetch" in window)) return;

  var ORIGIN = "https://www.narcoscope.com";
  var SLUG = "china-linked-precursor-incidents-official-record";
  var STORY_ID = "narcoscope.newsroom." + SLUG;
  var controller = "AbortController" in window ? new AbortController() : null;
  var timeout = window.setTimeout(function () {
    if (controller) controller.abort();
  }, 4500);

  function node(selector) { return root.querySelector(selector); }
  function set(selector, value) {
    var target = node(selector);
    if (target && value !== null && value !== undefined && String(value).trim()) {
      target.textContent = String(value);
    }
  }
  function safeUrl(value) {
    try {
      var url = new URL(String(value || ""), ORIGIN);
      if (url.origin !== ORIGIN || url.username || url.password) return null;
      url.searchParams.set("ref", "palimpsest_signal_relay");
      return url.toString();
    } catch (_) {
      return null;
    }
  }
  function boundedNumber(value) {
    return typeof value === "number" && isFinite(value) ? value : null;
  }
  function figure(dossier, id) {
    var figures = Array.isArray(dossier && dossier.keyFigures) ? dossier.keyFigures : [];
    return figures.find(function (item) { return item && item.id === id; }) || null;
  }
  function humanDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    var parsed = new Date(value + "T00:00:00Z");
    if (!isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) return null;
    return new Intl.DateTimeFormat("en-GB", {
      day: "numeric", month: "short", year: "numeric", timeZone: "UTC"
    }).format(parsed);
  }
  function getJson(url) {
    return fetch(url, {
      cache: "no-store",
      signal: controller ? controller.signal : undefined,
      headers: { "Accept": "application/json" }
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  getJson(ORIGIN + "/news/index.json")
    .then(function (index) {
      var article = index && index.schemaVersion === "narcoscope.newsroom.index.v1"
        && Array.isArray(index.articles) ? index.articles.find(function (item) {
          return item && item.id === STORY_ID && item.slug === SLUG;
        }) : null;
      var dossierUrl = safeUrl(article && article.dossierUrl);
      if (!article || !dossierUrl || new URL(dossierUrl).pathname !== "/news/" + SLUG + ".dossier.json") {
        throw new Error("No matching NarcoScope dossier");
      }
      return getJson(dossierUrl).then(function (dossier) {
        return { article: article, dossier: dossier };
      });
    })
    .then(function (loaded) {
      var article = loaded.article;
      var dossier = loaded.dossier;
      if (!dossier || dossier.schemaVersion !== "narcoscope.newsroom.evidence-analysis.v1"
          || dossier.slug !== SLUG || typeof dossier.title !== "string" || !dossier.title.trim()
          || typeof dossier.dek !== "string" || !dossier.dek.trim()) {
        throw new Error("Invalid NarcoScope dossier");
      }
      var dateNode = node("[data-ns-data-as-of]");
      var dateLabel = humanDate(dossier.dataAsOf);
      var previousDate = dateNode && dateNode.getAttribute("datetime");
      var safeStory = safeUrl(article && article.htmlUrl);
      if (!dateLabel || !humanDate(previousDate) || dossier.dataAsOf < previousDate
          || dossier.dataAsOf > new Date().toISOString().slice(0, 10)
          || !safeStory || new URL(safeStory).pathname !== "/news/" + SLUG + ".html") {
        throw new Error("Invalid NarcoScope identity or evidence date");
      }
      var incidents = figure(dossier, "china-eu-incident-count");
      var tonnes = figure(dossier, "china-eu-upper-bound-mass");
      var incidentValue = boundedNumber(incidents && incidents.value);
      var tonneValue = boundedNumber(tonnes && tonnes.value);
      var coverage = dossier.verificationReceipt && dossier.verificationReceipt.visualCitationCoverage;
      var percent = boundedNumber(coverage && coverage.percent);
      if (incidentValue === null || incidentValue < 0 || !Number.isInteger(incidentValue)
          || tonneValue === null || tonneValue < 0 || percent === null || percent < 0 || percent > 100
          || incidents.unit !== "incidents" || tonnes.unit !== "tonnes, nearly") {
        throw new Error("Incomplete NarcoScope figures");
      }

      // Validate the entire card before replacing any dated fallback field.
      set("[data-ns-title]", dossier.title);
      set("[data-ns-dek]", dossier.dek);
      set("[data-ns-feed-state]", "Latest dossier fetched");
      var storyLink = node("[data-ns-story-link]");
      if (storyLink) storyLink.href = safeStory;
      dateNode.dateTime = dossier.dataAsOf;
      dateNode.textContent = dateLabel;
      set("[data-ns-incidents]", incidentValue.toLocaleString("en-US"));
      set("[data-ns-tonnes]", "≈" + tonneValue.toLocaleString("en-US") + " t");
      set("[data-ns-citations]", percent + "%");
      root.setAttribute("data-relay-state", "remote");
    })
    .catch(function () {
      root.setAttribute("data-relay-state", "dated-fallback");
    })
    .finally(function () { window.clearTimeout(timeout); });
})();
