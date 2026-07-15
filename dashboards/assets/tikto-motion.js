/* ============================================================================
   TIKTÓ MOTION — the data-animation layer of the Tiktó system
   ----------------------------------------------------------------------------
   Design law: motion is signal, never decoration. Every animation here exists
   to preserve the reader's mental map (drill transitions), direct the eye along
   the data's own order (staggered entrances, ranked by value), or make a
   number's arrival legible (count-up, progressive line draw).

   Grounded in: Drillboards (arXiv 2410.12744) — adaptive dashboard hierarchy
   with animated transitions that preserve the mental map; DataSway
   (arXiv 2507.22051) — data-centric clip coordination, elements animate in the
   order the data ranks them; Apple HIG Motion — 150–450ms, spring easing,
   reduce-motion respected.

   Usage:  <script src="tikto-motion.js"></script> then TiktoMotion.init().
   Everything degrades to instant when prefers-reduced-motion is set, and the
   page renders fully without JS (hidden states apply only under html.tkm).
   ========================================================================== */
(function () {
  "use strict";
  const RM = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const SPRING = "cubic-bezier(0.22, 1, 0.36, 1)";
  const easeOut = (t) => 1 - Math.pow(1 - t, 3);

  /* ---- scroll-reveal: sections marked [data-tkm] rise in as they enter ---- */
  function reveal(root) {
    const els = (root || document).querySelectorAll("[data-tkm]:not(.tkm-in)");
    if (!els.length) return;
    if (RM || !("IntersectionObserver" in window)) {
      els.forEach((el) => el.classList.add("tkm-in"));
      return;
    }
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        e.target.classList.add("tkm-in");
        io.unobserve(e.target);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.05 });
    els.forEach((el) => io.observe(el));
  }

  /* ---- data-centric stagger: rank i → entrance delay, capped so long lists
         never feel slow (DataSway: order by data, not by layout) ------------ */
  function stagger(el, i, step) {
    el.classList.add("tkm-item");
    el.style.setProperty("--tkm-d", (Math.min(i, 12) * (step || 35)) + "ms");
  }

  /* ---- count-up: a hero number arrives, it never just appears ------------- */
  function countUp(el, value, opts) {
    const o = opts || {};
    const dec = o.decimals != null ? o.decimals : 0;
    const suffix = o.suffix || "";
    const done = () => { el.textContent = Number(value).toFixed(dec) + suffix; };
    if (RM || !isFinite(value)) { done(); return; }
    const dur = o.dur || 900, t0 = performance.now();
    (function frame(t) {
      const p = Math.min(1, (t - t0) / dur);
      el.textContent = (value * easeOut(p)).toFixed(dec) + suffix;
      if (p < 1) requestAnimationFrame(frame); else done();
    })(t0);
  }

  /* ---- progress driver: feed a canvas redraw a 0→1 value (line draw-in) ---- */
  function drawIn(fn, dur, el) {
    if (RM) { fn(1); return; }
    const run = () => {
      const t0 = performance.now();
      (function frame(t) {
        const p = Math.min(1, (t - t0) / (dur || 900));
        fn(easeOut(p));
        if (p < 1) requestAnimationFrame(frame);
      })(t0);
    };
    if (el && "IntersectionObserver" in window) {
      fn(0);
      const io = new IntersectionObserver((es) => {
        if (es.some((e) => e.isIntersecting)) { io.disconnect(); run(); }
      }, { threshold: 0.25 });
      io.observe(el);
    } else run();
  }

  /* ---- bar grow-in: elements marked data-tkm-bar sweep from 0 to their
         rendered width. Call AFTER innerHTML lands, in the same task, so the
         full-width state is never painted. ---------------------------------- */
  function growBars(root) {
    (root || document).querySelectorAll("[data-tkm-bar]").forEach((el) => {
      const w = el.style.width;
      if (RM || !w) return;
      el.style.transition = "none";
      el.style.width = "0%";
      requestAnimationFrame(() => requestAnimationFrame(() => {
        el.style.transition = "width 0.9s " + SPRING;
        el.style.width = w;
      }));
    });
  }

  /* ---- drill: animated expand/collapse that keeps the mental map ----------
     Height is measured (auto → px) so the fold is smooth, then cleared so the
     section stays responsive. Collapse also zeroes the section's own margin. */
  function drill(el, open) {
    if (RM) {
      el.style.display = open ? "" : "none";
      el.setAttribute("aria-hidden", open ? "false" : "true");
      return;
    }
    el.classList.add("tkm-drill");
    el.setAttribute("aria-hidden", open ? "false" : "true");
    if (open) {
      el.style.display = "";
      const h = el.scrollHeight;
      el.style.height = "0px";
      requestAnimationFrame(() => {
        el.classList.remove("tkm-drill--shut");
        el.style.height = h + "px";
        el.addEventListener("transitionend", function fin(e) {
          if (e.propertyName !== "height") return;
          el.style.height = "";
          el.removeEventListener("transitionend", fin);
        });
      });
    } else {
      el.style.height = el.scrollHeight + "px";
      requestAnimationFrame(() => {
        el.classList.add("tkm-drill--shut");
        el.style.height = "0px";
      });
    }
  }

  /* ---- boot: gate hidden states behind html.tkm so no-JS renders fully ---- */
  function init() {
    document.documentElement.classList.add("tkm");
    reveal(document);
  }

  window.TiktoMotion = { init, reveal, stagger, countUp, drawIn, drill, growBars, reduced: RM, SPRING };
})();
