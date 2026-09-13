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

  function mark(selector, state) {
    document.querySelectorAll(selector).forEach(function (node) {
      node.setAttribute("data-feed-state", state);
    });
  }

  read("/readings/eval-registry-latest.json").then(function (registry) {
    setText("[data-home-registry-runs]", registry.runs);
    setText("[data-home-registry-root]", String(registry.merkle_root || "").slice(0, 12));
    mark("[data-home-registry]", "live");
  }).catch(function () {
    mark("[data-home-registry]", "unavailable");
    setText("[data-home-registry-runs]", "unavailable");
    setText("[data-home-registry-root]", "Current receipt unavailable");
  });

  read("/readings/collector-health-latest.json", "palimpsest-collector-health.v1").then(function (board) {
    var summary = board.summary || {};
    var states = summary.by_state || {};
    var age = Date.now() - Date.parse(board.generated_at);
    if (!Number.isInteger(summary.n_datasets) || !Number.isInteger(states.fresh)
        || !Number.isFinite(age) || age < -120000 || age > 3600000) {
      throw new Error("invalid or stale collector-health counts");
    }
    setText("[data-home-osint-live]", states.fresh);
    setText("[data-home-osint-total]", summary.n_datasets);
    setText("[data-home-osint-state]", (states.stale || 0) + " stale; " + (states.gated || 0) + " gated; " + (states.partial || 0) + " partial");
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
