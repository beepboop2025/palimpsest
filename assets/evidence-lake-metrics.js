(function () {
  "use strict";

  var FEED = "/readings/evidence-lake-metrics-latest.json";
  var TOP_LEVEL_KEYS = [
    "schema",
    "generated_at",
    "edition",
    "summary",
    "lanes",
    "gates",
    "metrics_sha256"
  ];
  var SUMMARY_KEYS = [
    "analytical_rows",
    "publication_eligible_rows",
    "verified_source_bytes",
    "palimpsest_release_files",
    "telegram_corpus_records"
  ];
  var LANE_KEYS = [
    "id",
    "products",
    "queryable_records",
    "publication_eligible_records",
    "publication_state",
    "coverage",
    "citation"
  ];
  var CITATION_KEYS = ["label", "url"];
  var EXPECTED_LANES = [
    {
      id: "world-bank-wdi",
      products: ["palimpsest", "seiche"],
      publicationState: "allowed-with-attribution",
      publicationEligibleEqualsQueryable: true,
      coverageKeys: ["period", "indicators", "economies"],
      citation: {
        label: "World Bank, World Development Indicators",
        url: "https://datacatalog.worldbank.org/search/dataset/0037712/world-development-indicators"
      }
    },
    {
      id: "unodc-ids",
      products: ["narcoscope", "palimpsest"],
      publicationState: "allowed-with-citation",
      publicationEligibleEqualsQueryable: true,
      coverageKeys: ["period", "countries_or_territories", "drug_substances", "distinct_exact_content"],
      citation: {
        label: "UNODC Drugs Monitoring Platform",
        url: "https://dmpone.unodc.org/downloadIDS"
      }
    },
    {
      id: "ofr-stfm",
      products: ["liquilens", "seiche"],
      publicationState: "review-required",
      coverageKeys: ["series"],
      publicationEligibleRecords: 0,
      citation: {
        label: "U.S. Office of Financial Research, Short-term Funding Monitor",
        url: "https://www.financialresearch.gov/short-term-funding-monitor/api/"
      }
    },
    {
      id: "binance-public-archive",
      products: ["crypto"],
      publicationState: "manifest-only-pending-data-terms-review",
      coverageKeys: ["verified_manifest_files", "collected_payload_files"],
      queryableRecords: 0,
      publicationEligibleRecords: 0,
      citation: {
        label: "Binance public data archive documentation",
        url: "https://github.com/binance/binance-public-data"
      }
    }
  ];
  var GATE_KEYS = [
    "ofr_publication",
    "crypto_payload_collection",
    "telegram_corpus_collection",
    "ooni_bulk_collection",
    "common_crawl_bodies"
  ];
  var EXPECTED_GATES = {
    ofr_publication: "review-required",
    crypto_payload_collection: "disabled-pending-data-terms-review",
    telegram_corpus_collection: "blocked",
    ooni_bulk_collection: "blocked-pending-commercial-and-privacy-review",
    common_crawl_bodies: "not-collected"
  };

  function byId(id) { return document.getElementById(id); }

  function finiteCount(value) {
    return Number.isSafeInteger(value) && value >= 0;
  }

  function record(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
  }

  function exactKeys(value, expected, label) {
    if (!record(value)) throw new Error("invalid " + label);
    var actual = Object.keys(value).sort();
    var wanted = expected.slice().sort();
    if (actual.length !== wanted.length || actual.some(function (key, index) { return key !== wanted[index]; })) {
      throw new Error("invalid " + label + " keys");
    }
  }

  function exactStringArray(value, expected, label) {
    if (!Array.isArray(value) || value.length !== expected.length ||
        value.some(function (item, index) { return item !== expected[index]; })) {
      throw new Error("invalid " + label);
    }
  }

  function requireCount(value, label) {
    if (!finiteCount(value)) throw new Error("invalid " + label);
  }

  function validUtcTimestamp(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?Z$/.exec(value || "");
    if (!match) return false;
    var year = Number(match[1]);
    var month = Number(match[2]);
    var day = Number(match[3]);
    var hour = Number(match[4]);
    var minute = Number(match[5]);
    var second = Number(match[6]);
    var leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    var monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    return year >= 1 && month >= 1 && month <= 12 && day >= 1 &&
      day <= monthDays[month - 1] && hour <= 23 && minute <= 59 && second <= 59;
  }

  function validatePeriod(value, label) {
    if (!Array.isArray(value) || value.length !== 2) throw new Error("invalid " + label);
    value.forEach(function (year) {
      if (!Number.isSafeInteger(year) || year < 1800 || year > 2200) throw new Error("invalid " + label);
    });
    if (value[0] > value[1]) throw new Error("invalid " + label);
  }

  function validateCoverage(lane, spec) {
    var coverage = lane.coverage;
    exactKeys(coverage, spec.coverageKeys, spec.id + " coverage");
    if (spec.id === "world-bank-wdi") {
      validatePeriod(coverage.period, spec.id + " period");
      requireCount(coverage.indicators, spec.id + " indicators");
      requireCount(coverage.economies, spec.id + " economies");
      return;
    }
    if (spec.id === "unodc-ids") {
      validatePeriod(coverage.period, spec.id + " period");
      requireCount(coverage.countries_or_territories, spec.id + " countries_or_territories");
      requireCount(coverage.drug_substances, spec.id + " drug_substances");
      requireCount(coverage.distinct_exact_content, spec.id + " distinct_exact_content");
      return;
    }
    if (spec.id === "ofr-stfm") {
      requireCount(coverage.series, spec.id + " series");
      return;
    }
    requireCount(coverage.verified_manifest_files, spec.id + " verified_manifest_files");
    if (coverage.collected_payload_files !== 0) throw new Error("unreviewed crypto payload claim");
  }

  function validateCitation(citation, expected, laneId) {
    exactKeys(citation, CITATION_KEYS, laneId + " citation");
    if (citation.label !== expected.label || citation.url !== expected.url || citation.url.indexOf("https://") !== 0) {
      throw new Error("invalid " + laneId + " citation");
    }
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("en-US").format(value);
  }

  function formatBytes(value) {
    var units = ["B", "KB", "MB", "GB"];
    var size = value;
    var unit = 0;
    while (size >= 1000 && unit < units.length - 1) {
      size /= 1000;
      unit += 1;
    }
    return (unit === 0 ? String(size) : size.toFixed(size >= 100 ? 0 : 1)) + " " + units[unit];
  }

  function words(value) {
    return value.replace(/-/g, " ");
  }

  function element(name, className, text) {
    var node = document.createElement(name);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function sorted(value) {
    if (Array.isArray(value)) return value.map(sorted);
    if (value && typeof value === "object") {
      return Object.keys(value).sort().reduce(function (copy, key) {
        copy[key] = sorted(value[key]);
        return copy;
      }, {});
    }
    return value;
  }

  function toHex(buffer) {
    return Array.prototype.map.call(new Uint8Array(buffer), function (byte) {
      return byte.toString(16).padStart(2, "0");
    }).join("");
  }

  function verifyDigest(data) {
    if (!window.crypto || !window.crypto.subtle || !window.TextEncoder) {
      return Promise.reject(new Error("digest verification unavailable"));
    }
    var payload = {
      summary: data.summary,
      lanes: data.lanes,
      gates: data.gates
    };
    var bytes = new TextEncoder().encode(JSON.stringify(sorted(payload)) + "\n");
    return window.crypto.subtle.digest("SHA-256", bytes).then(function (digest) {
      if (toHex(digest) !== data.metrics_sha256) throw new Error("metrics digest mismatch");
      if (data.edition !== data.metrics_sha256.slice(0, 16)) throw new Error("edition mismatch");
      return data;
    });
  }

  function validate(data) {
    exactKeys(data, TOP_LEVEL_KEYS, "projection");
    if (data.schema !== "bulk.public-metrics.v1") throw new Error("unsupported schema");
    if (!validUtcTimestamp(data.generated_at)) throw new Error("invalid generated_at");
    if (!/^[0-9a-f]{16}$/.test(data.edition || "") || !/^[0-9a-f]{64}$/.test(data.metrics_sha256 || "")) {
      throw new Error("invalid projection identity");
    }
    if (!Array.isArray(data.lanes) || data.lanes.length !== EXPECTED_LANES.length) {
      throw new Error("incomplete projection");
    }
    exactKeys(data.summary, SUMMARY_KEYS, "summary");
    exactKeys(data.gates, GATE_KEYS, "gates");

    SUMMARY_KEYS.forEach(function (field) {
      requireCount(data.summary[field], "summary " + field);
    });

    var analytical = 0;
    var eligible = 0;
    data.lanes.forEach(function (lane, index) {
      var spec = EXPECTED_LANES[index];
      exactKeys(lane, LANE_KEYS, "lane " + index);
      if (lane.id !== spec.id) throw new Error("lane order drifted");
      exactStringArray(lane.products, spec.products, spec.id + " products");
      if (lane.publication_state !== spec.publicationState) throw new Error("invalid " + spec.id + " publication state");
      requireCount(lane.queryable_records, spec.id + " queryable_records");
      requireCount(lane.publication_eligible_records, spec.id + " publication_eligible_records");
      if (lane.publication_eligible_records > lane.queryable_records) throw new Error("eligible rows exceed queryable rows");
      if (spec.queryableRecords !== undefined && lane.queryable_records !== spec.queryableRecords) {
        throw new Error("invalid " + spec.id + " queryable_records");
      }
      if (spec.publicationEligibleRecords !== undefined &&
          lane.publication_eligible_records !== spec.publicationEligibleRecords) {
        throw new Error("invalid " + spec.id + " publication_eligible_records");
      }
      if (spec.publicationEligibleEqualsQueryable &&
          lane.publication_eligible_records !== lane.queryable_records) {
        throw new Error("invalid " + spec.id + " eligibility equality");
      }
      validateCoverage(lane, spec);
      validateCitation(lane.citation, spec.citation, spec.id);
      analytical += lane.queryable_records;
      eligible += lane.publication_eligible_records;
    });

    if (analytical !== data.summary.analytical_rows || eligible !== data.summary.publication_eligible_rows) {
      throw new Error("summary arithmetic drifted");
    }
    if (data.summary.telegram_corpus_records !== 0) throw new Error("unreviewed Telegram corpus claim");
    GATE_KEYS.forEach(function (key) {
      if (data.gates[key] !== EXPECTED_GATES[key]) throw new Error("gate drifted: " + key);
    });
    return data;
  }

  function coverageText(lane) {
    var coverage = lane.coverage;
    if (lane.id === "world-bank-wdi") {
      return formatNumber(coverage.economies) + " economies · " + formatNumber(coverage.indicators) +
        " indicators · " + coverage.period[0] + "–" + coverage.period[1];
    }
    if (lane.id === "unodc-ids") {
      return formatNumber(coverage.countries_or_territories) + " territories · " +
        formatNumber(coverage.drug_substances) + " substances · " + coverage.period[0] + "–" + coverage.period[1] +
        " · " + formatNumber(coverage.distinct_exact_content) + " exact-distinct";
    }
    if (lane.id === "ofr-stfm") return formatNumber(coverage.series) + " series";
    return formatNumber(coverage.collected_payload_files) + " payload files · " +
      formatNumber(coverage.verified_manifest_files) + " manifest file";
  }

  function renderLane(lane) {
    var row = element("article", "lake-lane");
    row.dataset.state = lane.publication_state;

    var source = element("div", "lake-lane__source");
    var citation = element("a", "lake-lane__title", lane.citation.label);
    citation.href = lane.citation.url;
    citation.target = "_blank";
    citation.rel = "noopener";
    source.appendChild(citation);
    source.appendChild(element("span", "lake-lane__coverage", coverageText(lane)));
    row.appendChild(source);

    var queryable = element("div", "lake-lane__number");
    queryable.appendChild(element("b", "", formatNumber(lane.queryable_records)));
    queryable.appendChild(element("span", "", "private queryable"));
    row.appendChild(queryable);

    var eligible = element("div", "lake-lane__number");
    eligible.appendChild(element("b", "", formatNumber(lane.publication_eligible_records)));
    eligible.appendChild(element("span", "", "publication-eligible"));
    row.appendChild(eligible);

    var state = element("div", "lake-lane__state");
    state.appendChild(element("b", "", words(lane.publication_state)));
    state.appendChild(element("span", "", lane.products.join(" · ")));
    row.appendChild(state);
    return row;
  }

  function render(data) {
    var ofr = data.lanes[2];
    byId("lake-private-rows").textContent = formatNumber(data.summary.analytical_rows);
    byId("lake-eligible-rows").textContent = formatNumber(data.summary.publication_eligible_rows);
    byId("lake-gated-rows").textContent = formatNumber(ofr.queryable_records - ofr.publication_eligible_records);
    byId("lake-telegram-rows").textContent = formatNumber(data.summary.telegram_corpus_records);
    byId("lake-source-bytes").textContent = formatBytes(data.summary.verified_source_bytes);
    byId("lake-release-files").textContent = formatNumber(data.summary.palimpsest_release_files);

    var lanes = byId("lake-lanes");
    lanes.textContent = "";
    data.lanes.forEach(function (lane) { lanes.appendChild(renderLane(lane)); });

    byId("lake-state").textContent = "Aggregate snapshot · metrics digest verified";
    byId("lake-state").dataset.state = "snapshot";
    byId("lake-asof").textContent = "Generated " + new Date(data.generated_at).toLocaleString() +
      " · edition " + data.edition + " · metrics digest verified in this browser. " +
      "Shared-secret source admission is enforced at import, not by this browser.";
    byId("lake-digest").textContent = data.metrics_sha256;
  }

  function fail() {
    byId("lake-state").textContent = "Metrics unavailable · no corpus-size claim shown";
    byId("lake-state").dataset.state = "unavailable";
    byId("lake-asof").textContent = "The aggregate projection could not be loaded or digest-verified.";
    byId("lake-lanes").textContent = "";
    byId("lake-lanes").appendChild(element("p", "lake-empty", "No evidence-lake count is displayed without a valid closed-schema projection."));
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { validate: validate, verifyDigest: verifyDigest };
  }
  if (typeof document === "undefined" || !byId("evidence-lake")) return;
  fetch(FEED, { cache: "no-store", credentials: "omit" })
    .then(function (response) {
      if (!response.ok) throw new Error("evidence lake " + response.status);
      return response.json();
    })
    .then(validate)
    .then(verifyDigest)
    .then(render)
    .catch(fail);
}());
