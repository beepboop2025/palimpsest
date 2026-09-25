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

  function read(url, expectedSchema, allowStaleStatus) {
    return fetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok && !(allowStaleStatus && response.status === 503)) {
        throw new Error("HTTP " + response.status);
      }
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
    if (!isPublicDocument(feed) || feed.version !== "https://jsonfeed.org/version/1.1") {
      throw new Error("invalid public source feed");
    }
    items = Array.isArray(feed.items) ? feed.items : [];
    reports = items.filter(function (item) {
      return item && item._palimpsest && item._palimpsest.kind === "publisher_source_record";
    });
    if (!reports.length) throw new Error("empty public source feed");
    return reports.length;
  }

  function publicationView(freshness, now) {
    if (!isPublicDocument(freshness, "palimpsest.publication-freshness.v1")
        || !["fresh", "stale"].includes(freshness.status)
        || !/^[a-f0-9]{40}$/.test(freshness.source_commit || "")
        || !/^[a-f0-9]{64}$/.test(freshness.tree_sha256 || "")) {
      throw new Error("invalid publication receipt");
    }
    var checked = Date.parse(freshness.checked_at);
    if (!Number.isFinite(checked) || now - checked < -120000 || now - checked > 300000) {
      throw new Error("invalid publication check time");
    }
    var clocks = freshness.clocks || {};
    var live = freshness.status === "fresh";
    ["publication", "wire"].forEach(function (name) {
      var clock = clocks[name] || {};
      var age = now - Date.parse(clock.generated_at);
      var budget = name === "wire" ? 1800 : 3600;
      if (!Number.isFinite(age) || age < -120000
          || !["fresh", "stale"].includes(clock.status)) {
        throw new Error("invalid publication clock");
      }
      live = live && clock.status === "fresh" && age <= budget * 1000;
    });
    return {
      state: live ? "live" : "delayed",
      asOf: new Date(clocks.wire.generated_at).toISOString().slice(0, 16).replace("T", " ") + " UTC",
      identity: freshness.source_commit + ":" + freshness.tree_sha256
    };
  }

  function collectorView(board, now) {
    if (!isPublicDocument(board, "palimpsest-collector-health.v1")) throw new Error("invalid collector receipt");
    var summary = board.summary || {};
    var states = summary.by_state || {};
    var age = now - Date.parse(board.generated_at);
    if (!Number.isInteger(summary.n_datasets) || summary.n_datasets < 1
        || !Number.isInteger(states.fresh) || states.fresh < 0 || states.fresh > summary.n_datasets
        || !Number.isFinite(age) || age < -120000
        || !Object.values(states).every(function (count) { return Number.isInteger(count) && count >= 0; })
        || Object.values(states).reduce(function (sum, count) { return sum + count; }, 0) !== summary.n_datasets) {
      throw new Error("invalid collector counts");
    }
    return { state: age <= 3600000 ? "live" : "delayed", fresh: states.fresh, total: summary.n_datasets,
      asOf: new Date(board.generated_at).toISOString().slice(0, 16).replace("T", " ") + " UTC",
      coverage: (states.stale || 0) + " stale; " + (states.gated || 0) + " gated; " + (states.partial || 0) + " partial" };
  }

  if (typeof module === "object" && module && module.exports) {
    module.exports = {
      countPublicReports: countPublicReports,
      publicationView: publicationView,
      collectorView: collectorView,
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

  var collector = null;
  var collecting = false;
  function renderCollector(view, checkUnavailable) {
    var current = view.state === "live" && !checkUnavailable;
    setText("[data-home-osint-label]", current ? "Fresh datasets" : "Fresh at last assessment");
    setText("[data-home-osint-live]", view.fresh);
    setText("[data-home-osint-total]", view.total);
    setText("[data-home-osint-state]", "As of " + view.asOf + ". "
      + (checkUnavailable ? "Refresh unavailable. " : current ? "" : "Update pending. ") + view.coverage);
    mark("[data-home-osint]", current ? "live" : "delayed");
  }
  function refreshCollector() {
    if (collecting || document.hidden) return;
    collecting = true;
    read("/readings/collector-health-latest.json", "palimpsest-collector-health.v1").then(function (board) {
      collector = collectorView(board, Date.now());
      renderCollector(collector, false);
    }).catch(function () {
      if (collector) return renderCollector(collector, true);
      mark("[data-home-osint]", "unavailable");
      setText("[data-home-osint-live]", "unavailable");
      setText("[data-home-osint-total]", "unavailable");
      setText("[data-home-osint-state]", "Assessment unavailable");
    }).finally(function () { collecting = false; });
  }

  var published = null;
  var refreshing = false;

  function readPublication() {
    // A 503 freshness receipt describes delayed publication; it is not a data feed.
    return read("/freshness", "palimpsest.publication-freshness.v1", true).then(function (receipt) {
      return publicationView(receipt, Date.now());
    });
  }

  function renderPublished(view, checkUnavailable) {
    setText("[data-home-wire-events]", published.count);
    setText("[data-home-wire-label]", "Published reports");
    setText("[data-home-wire-source-state]", "As of " + view.asOf + ". "
      + (checkUnavailable ? "Freshness check unavailable. " : view.state === "live" ? "Current publication. " : "Update pending. ")
      + "Public metadata feed; per-source coverage withheld.");
    mark("[data-home-wire]", checkUnavailable ? "delayed" : view.state);
  }

  function refreshPublished() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    readPublication().then(function (before) {
      if (published && published.view.identity === before.identity) return before;
      return read("/news/feed.json").then(function (feed) {
        var count = countPublicReports(feed);
        return readPublication().then(function (after) {
          if (before.identity !== after.identity) throw new Error("publication changed during count");
          published = { count: count, view: after };
          return after;
        });
      });
    }).then(function (view) {
      published.view = view;
      renderPublished(view, false);
    }).catch(function () {
      if (published) {
        renderPublished(published.view, true);
      } else {
        mark("[data-home-wire]", "unavailable");
        setText("[data-home-wire-label]", "Published reports");
        setText("[data-home-wire-events]", "unavailable");
        setText("[data-home-wire-source-state]", "Publication receipt unavailable. Open the source index to inspect individual reports.");
      }
    }).finally(function () { refreshing = false; });
  }

  function refreshCounts() { refreshPublished(); refreshCollector(); }
  refreshCounts();
  setInterval(refreshCounts, 60000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshCounts();
  });
}());
