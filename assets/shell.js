/* ============================================================================
   PALIMPSEST SHELL — behaviour
   ----------------------------------------------------------------------------
   Progressive enhancement only. The nav markup is real HTML stamped into every
   page by scripts/sync_nav.py, so this file never creates navigation — it only
   makes existing navigation feel like a surface rather than a list of links.

   Everything here is optional. With JS off you get a plain, fully usable nav
   (every flyout is just a visible group), and with prefers-reduced-motion the
   scroll work short-circuits to "show it".
   ========================================================================== */
(function () {
  "use strict";

  var RM = matchMedia("(prefers-reduced-motion: reduce)").matches;
  var NATIVE_TIMELINE = CSS.supports && CSS.supports("animation-timeline", "view()");

  /* ---------------------------------------------------------------- nav ---- */
  /* Flyouts open on hover with intent (a small delay stops them flickering as
     the pointer crosses the bar) and on click/keyboard without any delay. */
  function initNav() {
    var nav = document.querySelector(".ps-nav");
    if (!nav) return;

    var items = [].slice.call(nav.querySelectorAll(".ps-nav__item"));
    var openTimer = null, closeTimer = null;
    var isTouch = matchMedia("(hover: none)").matches;
    var isCompact = function () { return matchMedia("(max-width: 940px)").matches; };

    function close(item) {
      item.removeAttribute("data-open");
      var t = item.querySelector("[aria-expanded]");
      if (t) t.setAttribute("aria-expanded", "false");
    }
    function closeAll(except) {
      items.forEach(function (i) { if (i !== except) close(i); });
    }
    function open(item) {
      closeAll(item);
      item.setAttribute("data-open", "");
      var t = item.querySelector("[aria-expanded]");
      if (t) t.setAttribute("aria-expanded", "true");
    }

    items.forEach(function (item) {
      var trigger = item.querySelector("[aria-expanded]");
      var panel = item.querySelector(".ps-flyout");
      if (!trigger || !panel) return;

      trigger.addEventListener("click", function (e) {
        e.preventDefault();
        if (item.hasAttribute("data-open")) close(item); else open(item);
      });

      if (!isTouch) {
        item.addEventListener("pointerenter", function () {
          if (isCompact()) return;
          clearTimeout(closeTimer);
          openTimer = setTimeout(function () { open(item); }, 90);
        });
        item.addEventListener("pointerleave", function () {
          if (isCompact()) return;
          clearTimeout(openTimer);
          closeTimer = setTimeout(function () { close(item); }, 180);
        });
      }

      /* Keyboard: the flyout is a menu, so it closes on Escape and returns
         focus to the trigger rather than dumping the user at the top. */
      item.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && item.hasAttribute("data-open")) {
          close(item); trigger.focus();
        }
      });
      /* Tabbing out of the group closes it. */
      item.addEventListener("focusout", function (e) {
        if (isCompact()) return;
        if (!item.contains(e.relatedTarget)) close(item);
      });
    });

    document.addEventListener("click", function (e) {
      if (!nav.contains(e.target)) closeAll(null);
    });

    /* ---- mobile sheet ---- */
    var burger = nav.querySelector(".ps-nav__burger");
    if (burger) {
      burger.addEventListener("click", function () {
        var openNow = document.body.hasAttribute("data-ps-menu");
        if (openNow) {
          document.body.removeAttribute("data-ps-menu");
          burger.setAttribute("aria-expanded", "false");
          closeAll(null);
        } else {
          document.body.setAttribute("data-ps-menu", "");
          burger.setAttribute("aria-expanded", "true");
        }
      });
      var scrim = nav.querySelector(".ps-nav__scrim");
      if (scrim) scrim.addEventListener("click", function () { burger.click(); });
      addEventListener("keydown", function (e) {
        if (e.key === "Escape" && document.body.hasAttribute("data-ps-menu")) burger.click();
      });
    }

    /* ---- scrolled state: hairline + lift appear once content is behind it --- */
    var ticking = false;
    function onScroll() {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () {
        if (scrollY > 8) nav.setAttribute("data-scrolled", "");
        else nav.removeAttribute("data-scrolled");
        ticking = false;
      });
    }
    addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  /* ------------------------------------------------------------- reveal ---- */
  /* Only runs when the browser lacks scroll-driven animations. When it has
     them, CSS owns the reveal entirely and this does nothing at all. */
  function initReveal() {
    var els = [].slice.call(document.querySelectorAll(".ps-reveal, .ps-reveal--deep"));
    if (!els.length) return;

    if (NATIVE_TIMELINE && !RM) return;           /* CSS is handling it */

    if (RM || !("IntersectionObserver" in window)) return;  /* leave visible */

    document.documentElement.classList.add("ps-js");
    els.forEach(function (el) { el.classList.add("ps-fallback-reveal"); });

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add("ps-in");
        io.unobserve(e.target);
      });
    }, { rootMargin: "0px 0px -6% 0px", threshold: 0.04 });

    els.forEach(function (el) { io.observe(el); });
  }

  /* ------------------------------------------------------------ stagger ---- */
  /* Give each child of a .ps-stagger its index, so CSS can turn position into
     delay. Capped at 12 so a 200-row table does not animate for nine seconds. */
  function initStagger() {
    document.querySelectorAll(".ps-stagger").forEach(function (list) {
      [].slice.call(list.children).forEach(function (child, i) {
        child.style.setProperty("--ps-i", Math.min(i, 12));
      });
    });
  }

  /* --------------------------------------------------------- count-up ------ */
  /* A published number arrives rather than simply being there. Exposed so the
     signal pages can call it after their fetch resolves. */
  function countUp(el, value, opts) {
    opts = opts || {};
    var dec = opts.decimals != null ? opts.decimals : 0;
    var suffix = opts.suffix || "";
    var v = Number(value);
    function settle() { el.textContent = (isFinite(v) ? v.toFixed(dec) : "—") + suffix; }
    if (RM || !isFinite(v)) { settle(); return; }
    var dur = opts.dur || 780, t0 = performance.now();
    requestAnimationFrame(function frame(t) {
      var p = Math.min(1, (t - t0) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = (v * eased).toFixed(dec) + suffix;
      if (p < 1) requestAnimationFrame(frame); else settle();
    });
  }

  /* ------------------------------------------------------------- helpers --- */
  /* Shared by every signal page: fetch a reading, render it, and fail loudly
     rather than leaving a plausible-looking empty panel on screen. A blank
     number on this site would be a lie by omission. */
  function loadReading(url, onData, onError) {
    return fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(onData)
      .catch(function (err) {
        if (onError) onError(err);
        else console.error("[palimpsest] could not load " + url, err);
      });
  }

  /* "3 hours ago", for freshness lines. Staleness is a finding, not a detail:
     past a day the caller should be showing it as a warning, not a timestamp. */
  function ago(iso) {
    var then = Date.parse(iso);
    if (!isFinite(then)) return "unknown";
    var s = Math.max(0, (Date.now() - then) / 1000);
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 172800) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }

  function init() {
    initNav();
    initStagger();
    initReveal();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.PalimpsestShell = {
    init: init,
    countUp: countUp,
    loadReading: loadReading,
    ago: ago,
    reducedMotion: RM
  };
})();
