(() => {
  "use strict";

  const cards = Array.from(document.querySelectorAll(".situation-card"));
  const search = document.querySelector("#situation-search");
  const posture = document.querySelector("#situation-posture");
  const count = document.querySelector("#situation-count");
  const empty = document.querySelector("#situation-empty");
  const deskButtons = Array.from(document.querySelectorAll("[data-situation-desk]"));
  let desk = "all";

  if (!cards.length || !search || !posture || !count || !empty) return;

  const apply = () => {
    const query = search.value.trim().toLocaleLowerCase();
    const requestedPosture = posture.value;
    let visible = 0;

    cards.forEach((card) => {
      const matchesDesk = desk === "all" || card.dataset.desk === desk;
      const matchesPosture =
        requestedPosture === "all" || card.dataset.posture === requestedPosture;
      const matchesSearch = !query || (card.dataset.search || "").includes(query);
      const show = matchesDesk && matchesPosture && matchesSearch;
      card.hidden = !show;
      if (show) visible += 1;
    });

    count.textContent =
      `Showing ${visible} situation${visible === 1 ? "" : "s"} on this archive page`;
    empty.hidden = visible !== 0;
  };

  deskButtons.forEach((button) => {
    button.addEventListener("click", () => {
      desk = button.dataset.situationDesk || "all";
      deskButtons.forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", active ? "true" : "false");
      });
      apply();
    });
  });

  search.addEventListener("input", apply);
  posture.addEventListener("change", apply);
})();
