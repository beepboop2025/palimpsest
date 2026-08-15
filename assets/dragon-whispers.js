(() => {
  "use strict";

  const search = document.querySelector("#dragon-whispers-search");
  const entries = Array.from(document.querySelectorAll(".dw-entry"));
  const buttons = Array.from(document.querySelectorAll("[data-whisper-tier]"));
  const count = document.querySelector("#dragon-whispers-count");
  const empty = document.querySelector("#dragon-whispers-empty-filter");
  if (!search || !count || !empty) return;

  let tier = "all";
  const normalize = (value) => value.trim().toLocaleLowerCase();

  const apply = () => {
    const query = normalize(search.value);
    let visible = 0;
    entries.forEach((entry) => {
      const matchesTier = tier === "all" || entry.dataset.tier === tier;
      const matchesText = !query || normalize(entry.dataset.search || "").includes(query);
      const show = matchesTier && matchesText;
      entry.hidden = !show;
      if (show) visible += 1;
    });
    count.textContent = `Showing ${visible} of ${entries.length} reviewed whispers`;
    empty.hidden = visible !== 0 || entries.length === 0;
  };

  search.addEventListener("input", apply);
  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      tier = button.dataset.whisperTier || "all";
      buttons.forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      apply();
    });
  });
})();
