(function () {
  "use strict";

  function normalize(value) {
    return String(value || "").trim().toLocaleLowerCase();
  }

  function initializeLedgerControls(controls) {
    var scope = controls.parentElement;
    var search = controls.querySelector("[data-cn-search]");
    var filter = controls.querySelector("[data-cn-filter]");
    var count = controls.querySelector("[data-cn-result-count]");
    var records = Array.prototype.slice.call(
      scope.querySelectorAll("[data-cn-record]")
    );
    if (!search || !filter || !count || !records.length) return false;

    function update() {
      var query = normalize(search.value);
      var wantedStatus = normalize(filter.value);
      var visible = 0;

      records.forEach(function (record) {
        var retainedText = normalize(record.getAttribute("data-cn-text"));
        var readableText = normalize(record.textContent);
        var status = normalize(record.getAttribute("data-cn-status"));
        var matchesQuery = !query || retainedText.indexOf(query) !== -1 ||
          readableText.indexOf(query) !== -1;
        var matchesStatus = wantedStatus === "all" || status === wantedStatus;
        var show = matchesQuery && matchesStatus;
        record.hidden = !show;
        if (show) visible += 1;
      });

      count.textContent = visible + " of " + records.length + " visible";
    }

    search.addEventListener("input", update);
    filter.addEventListener("change", update);
    update();
    return true;
  }

  function initialize() {
    var controls = Array.prototype.slice.call(
      document.querySelectorAll("[data-cn-controls]")
    );
    var ready = controls.some(initializeLedgerControls);
    if (ready) document.documentElement.classList.add("cn-js");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }
}());
