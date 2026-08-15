(() => {
  "use strict";

  const search = document.querySelector("#china-stream-search");
  const entries = Array.from(document.querySelectorAll(".cs-entry"));
  const buttons = Array.from(document.querySelectorAll("[data-desk-filter]"));
  const count = document.querySelector("#china-stream-count");
  const empty = document.querySelector("#china-stream-empty");
  if (!search || !entries.length || !count || !empty) return;

  let desk = "all";
  const normalize = (value) => value.trim().toLocaleLowerCase();

  const apply = () => {
    const query = normalize(search.value);
    let visible = 0;
    entries.forEach((entry) => {
      const matchesDesk = desk === "all" || entry.dataset.desk === desk;
      const matchesText = !query || normalize(entry.dataset.search || "").includes(query);
      const show = matchesDesk && matchesText;
      entry.hidden = !show;
      if (show) visible += 1;
    });
    count.textContent = `Showing ${visible} of ${entries.length} dispatches on this page`;
    empty.hidden = visible !== 0;
  };

  search.addEventListener("input", apply);
  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      desk = button.dataset.deskFilter || "all";
      buttons.forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      apply();
    });
  });
})();
