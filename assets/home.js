(function () {
  "use strict";

  function isPublicDocument(document, expectedSchema) {
    if (!document || typeof document !== "object" || Array.isArray(document)) return false;
    if (document.status === "restricted" || document.status === "unavailable") return false;
    if (document.availability === "unavailable" || document.availability === "restricted") return false;
    if (document.publication_allowed === false) return false;
    if (expectedSchema && document.schema_version !== expectedSchema) return false;
    return true;
  }

  function read(url, expectedSchema) {
    return fetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (document) {
      if (!isPublicDocument(document, expectedSchema)) {
        throw new Error("restricted or invalid public document");
      }
      return document;
    });
  }

  function countPublicReports(feed) {
    var items;
    var reports;
    if (!feed || feed.version !== "https://jsonfeed.org/version/1.1") {
      throw new Error("invalid public source feed");
    }
    items = Array.isArray(feed.items) ? feed.items : [];
    reports = items.filter(function (item) {
      return item && item._palimpsest && item._palimpsest.kind === "publisher_source_record";
    });
    if (!reports.length) throw new Error("empty public source feed");
    return reports.length;
  }

  if (typeof module === "object" && module && module.exports) {
    module.exports = {
      countPublicReports: countPublicReports,
      isPublicDocument: isPublicDocument
    };
    return;
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

  read("/readings/eval-articles-latest.json", "palimpsest-eval-journal.v1").then(function (edition) {
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

  read("/readings/osint-china-latest.json", "osint-china.v1").then(function (board) {
    if (!Number.isInteger(board.n_signals_live) || !Number.isInteger(board.n_signals_total)) {
      throw new Error("invalid OSINT counts");
    }
    setText("[data-home-osint-live]", board.n_signals_live);
    setText("[data-home-osint-total]", board.n_signals_total);
    setText("[data-home-osint-state]", board.health && board.health.status || "unknown");
    mark("[data-home-osint]", "live");
  }).catch(function () {
    mark("[data-home-osint]", "unavailable");
    setText("[data-home-osint-summary]", "Counts unavailable");
    setText("[data-home-osint-state]", "restricted or unavailable");
  });

  read("/freshness", "palimpsest.publication-freshness.v1").then(function (freshness) {
    if (freshness.status !== "fresh") throw new Error("stale publication");
    return read("/readings/newswire-latest.json", "palimpsest-newswire.v1").then(function (wire) {
      var coverage = wire.coverage || {};
      if (!Number.isInteger(wire.n_events) || !Number.isInteger(coverage.successful_sources) || !Number.isInteger(coverage.registry_sources)) {
        throw new Error("invalid source-index counts");
      }
      setText("[data-home-wire-events]", wire.n_events);
      setText("[data-home-wire-sources]", coverage.successful_sources);
      setText("[data-home-wire-total-sources]", coverage.registry_sources);
      mark("[data-home-wire]", "live");
    }).catch(function () {
      return read("/news/feed.json").then(function (feed) {
        var reports = countPublicReports(feed);
        setText("[data-home-wire-summary]", reports + " grouped reports");
        setText("[data-home-wire-source-state]", "Public metadata feed; per-source coverage withheld");
        mark("[data-home-wire]", "live");
      });
    });
  }).catch(function () {
    mark("[data-home-wire]", "unavailable");
    setText("[data-home-wire-summary]", "Current report count unavailable");
    setText("[data-home-wire-source-state]", "Live check unavailable");
  });
}());
