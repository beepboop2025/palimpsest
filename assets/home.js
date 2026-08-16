(function () {
  "use strict";

  function read(url) {
    return fetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  function setText(selector, value) {
    if (value === undefined || value === null || value === "") return;
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
    var numbers;
    var limitations;
    if (!lead) throw new Error("empty findings edition");

    numbers = Array.isArray(lead.key_numbers) ? lead.key_numbers : [];
    limitations = Array.isArray(lead.limitations) ? lead.limitations : [];
    setText("[data-home-journal-title]", lead.title);
    setText("[data-home-journal-dek]", lead.dek);
    setText("[data-home-journal-state]", "Verified " + dateLabel(lead.updated_at));
    setText("[data-home-journal-revision]", lead.revision_id);
    setText("[data-home-journal-disclosure]", lead.disclosure);
    if (limitations[0]) setText("[data-home-journal-limit]", limitations[0].text);
    if (numbers[0]) {
      setText('[data-home-journal-number="0"]', numbers[0].value);
      setText('[data-home-journal-label="0"]', numbers[0].label);
    }
    setHref("[data-home-journal-link]", lead.url);
    mark("[data-home-journal]", "live");
  }).catch(function () {
    mark("[data-home-journal]", "unavailable");
    setText("[data-home-journal-state]", "Live check unavailable; dated result shown");
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
    var coverage = wire.coverage || {};
    setText("[data-home-wire-events]", wire.n_events);
    setText("[data-home-wire-sources]", coverage.successful_sources);
    setText("[data-home-wire-total-sources]", coverage.registry_sources);
    mark("[data-home-wire]", "live");
  }).catch(function () {
    mark("[data-home-wire]", "unavailable");
  });
}());
