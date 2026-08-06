/* Palimpsest developer-page interactions. Progressive enhancement only: every
   command and endpoint remains readable and copyable when JavaScript is off. */
(function () {
  "use strict";

  var endpoint = document.body.dataset.mcpEndpoint;
  var status = document.getElementById("mcp-status");
  var output = document.getElementById("mcp-output");
  var check = document.getElementById("check-mcp");
  var run = document.getElementById("run-verdict");

  function selectText(el) {
    var range = document.createRange();
    range.selectNodeContents(el);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var source = document.getElementById(button.dataset.copy);
      if (!source) return;
      var value = source.textContent.trim();
      var copied = navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(value)
        : Promise.reject(new Error("clipboard unavailable"));
      copied.then(function () {
        button.setAttribute("data-copied", "");
        button.textContent = "Copied";
      }).catch(function () {
        selectText(source);
        button.textContent = "Selected — press copy";
      }).finally(function () {
        window.setTimeout(function () {
          button.removeAttribute("data-copied");
          button.textContent = button.dataset.original || "Copy";
        }, 1800);
      });
      if (!button.dataset.original) button.dataset.original = button.textContent;
    });
  });

  function setBusy(button, busy) {
    button.disabled = busy;
    button.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function show(state, message, value) {
    status.dataset.state = state;
    status.textContent = message;
    if (value !== undefined) {
      output.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    }
  }

  check.addEventListener("click", function () {
    setBusy(check, true);
    show("", "Checking the production MCP catalog…");
    fetch(endpoint, { headers: { "Accept": "application/json" }, cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (body) {
        var count = Array.isArray(body.tools) ? body.tools.length : 0;
        show("ok", "Connected. The server advertises " + count + " read-only tools.", body);
      })
      .catch(function (error) {
        show("error", "Could not reach the MCP server: " + error.message);
      })
      .finally(function () { setBusy(check, false); });
  });

  run.addEventListener("click", function () {
    setBusy(run, true);
    show("", "Calling whats_happening on the production server…");
    fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: "palimpsest-developer-console",
        method: "tools/call",
        params: { name: "whats_happening", arguments: {} }
      })
    })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (body) {
        if (body.error) throw new Error(body.error.message || "JSON-RPC error");
        var result = body.result && (body.result.structuredContent || body.result);
        show("ok", "Live verdict returned. Keep its timestamps and caveats when citing it.", result);
      })
      .catch(function (error) {
        show("error", "The live call failed: " + error.message);
      })
      .finally(function () { setBusy(run, false); });
  });
})();
