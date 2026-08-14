(function () {
  "use strict";

  function read(url) {
    return fetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  function setText(selector, value) {
    document.querySelectorAll(selector).forEach(function (node) {
      node.textContent = String(value);
    });
  }

  function setHref(selector, value) {
    if (typeof value !== "string" || value.charAt(0) !== "/") return;
    document.querySelectorAll(selector).forEach(function (node) {
      node.setAttribute("href", value);
    });
  }

  function dateLabel(value) {
    var timestamp = Date.parse(value);
    if (!isFinite(timestamp)) return "dated edition";
    return new Intl.DateTimeFormat("en", {
      day: "numeric",
      month: "short",
      year: "numeric",
      timeZone: "UTC"
    }).format(new Date(timestamp));
  }

  function mark(selector, state) {
    document.querySelectorAll(selector).forEach(function (node) {
      node.setAttribute("data-feed-state", state);
    });
  }

  read("/readings/eval-articles-latest.json").then(function (edition) {
    var articles = Array.isArray(edition.articles) ? edition.articles : [];
    var lead = articles[0];
    if (!lead) throw new Error("empty journal edition");
    setText("[data-home-journal-title]", lead.title);
    setText("[data-home-journal-dek]", lead.dek);
    setText("[data-home-journal-date]", "Updated " + dateLabel(lead.updated_at));
    setHref("[data-home-journal-link]", lead.url);
    lead.key_numbers.slice(0, 3).forEach(function (number, index) {
      setText('[data-home-journal-number="' + index + '"]', number.value);
      setText('[data-home-journal-label="' + index + '"]', number.label);
    });
    mark("[data-home-journal]", "live");
  }).catch(function () {
    mark("[data-home-journal]", "unavailable");
    setText("[data-home-journal-date]", "Structured edition unavailable");
  });

  read("/readings/eval-registry-latest.json").then(function (registry) {
    setText("[data-home-registry-runs]", registry.runs);
    setText("[data-home-registry-root]", String(registry.merkle_root || "").slice(0, 12));
    mark("[data-home-registry]", "live");
  }).catch(function () {
    mark("[data-home-registry]", "unavailable");
  });

  read("/readings/osint-china-latest.json").then(function (board) {
    setText("[data-home-osint-live]", board.n_signals_live);
    setText("[data-home-osint-total]", board.n_signals_total);
    setText("[data-home-osint-state]", board.health && board.health.status || "unknown");
    mark("[data-home-osint]", "live");
  }).catch(function () {
    mark("[data-home-osint]", "unavailable");
  });

  read("/readings/newswire-latest.json").then(function (wire) {
    setText("[data-home-wire-events]", wire.n_events);
    setText("[data-home-wire-date]", dateLabel(wire.generated_at));
    mark("[data-home-wire]", "live");
  }).catch(function () {
    mark("[data-home-wire]", "unavailable");
  });
}());
