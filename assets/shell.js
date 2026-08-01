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

  /* -------------------------------------------------------- page share ---- */
  /* Every reading, brief and dashboard can leave the site as a social-grade
     card: the brand row, the page's own h1 and description, and a footer
     carrying the link and the export stamp. Self-contained on purpose, so
     pages without the chart kit still share. */
  function shareTokens() {
    var cs = getComputedStyle(document.documentElement);
    var v = function (name, fb) {
      var x = cs.getPropertyValue(name).trim();
      return x || fb;
    };
    return {
      text0: v("--tk-text-0", "#ffffff"),
      text2: v("--tk-text-2", "#94a3b8"),
      text3: v("--tk-text-3", "#7d8ca3"),
      text4: v("--tk-text-4", "#6f8098"),
      grid: v("--tk-line-1", "#1a1a1a"),
      grid2: v("--tk-line-2", "#272727"),
      mono: v("--tk-font-mono", "ui-monospace, Menlo, monospace")
    };
  }
  /* Flatten the translucent surface stack over an opaque base; a card filled
     with a rgba wash would ship as a transparent png. */
  function shareSurface(el) {
    var parse = function (c) {
      var m = /rgba?\(([^)]+)\)/.exec(c || "");
      if (!m) return null;
      var p = m[1].split(",").map(parseFloat);
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    };
    var layers = [];
    var n = el;
    while (n) {
      var c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0) layers.push(c);
      n = n.parentElement;
    }
    var r = 10, g = 10, b = 12;
    for (var i = layers.length - 1; i >= 0; i--) {
      var q = layers[i];
      r = q.r * q.a + r * (1 - q.a);
      g = q.g * q.a + g * (1 - q.a);
      b = q.b * q.a + b * (1 - q.a);
    }
    return "rgb(" + Math.round(r) + "," + Math.round(g) + "," + Math.round(b) + ")";
  }
  function shareWrap(ctx, text, x, y, maxW, lineH, maxLines, paint) {
    var words = String(text).split(/\s+/).filter(Boolean);
    var line = "", lines = 0;
    for (var i = 0; i < words.length; i++) {
      var probe = line ? line + " " + words[i] : words[i];
      if (ctx.measureText(probe).width > maxW && line) {
        if (lines === maxLines - 1) {
          while (line && ctx.measureText(line + "…").width > maxW) line = line.replace(/\s*\S+$/, "");
          if (paint) ctx.fillText(line + "…", x, y);
          return y;
        }
        if (paint) ctx.fillText(line, x, y);
        y += lineH;
        lines += 1;
        line = words[i];
      } else {
        line = probe;
      }
    }
    if (line && paint) ctx.fillText(line, x, y);
    return y;
  }
  var SHARE_ICON = null;
  function shareIcon() {
    if (!SHARE_ICON) {
      SHARE_ICON = new Promise(function (res) {
        var img = new Image();
        img.onload = function () { res(img); };
        img.onerror = function () { res(null); };
        img.src = "/brand/palimpsest-icon-512.png";
      });
    }
    return SHARE_ICON;
  }
  function composePageCard(meta) {
    var W = 1200, PAD = 48;
    return shareIcon().then(function (icon) {
      var t = shareTokens();
      var probe = document.createElement("canvas").getContext("2d");
      probe.font = "650 30px " + t.mono;
      var titleTop = 170;
      var y = shareWrap(probe, meta.title, PAD, titleTop, W - PAD * 2, 42, 3, false) + 22;
      var bodyTop = 0;
      if (meta.body) {
        probe.font = "15px " + t.mono;
        bodyTop = y + 20;
        y = shareWrap(probe, meta.body, PAD, bodyTop, W - PAD * 2 - 8, 26, 5, false);
      }
      var H = Math.max(y + 104, 420);
      var ruleY = H - 68; /* the footer sits on the card's floor, never adrift */

      var EX = 2;
      var cv = document.createElement("canvas");
      cv.width = W * EX;
      cv.height = H * EX;
      var ctx = cv.getContext("2d");
      ctx.scale(EX, EX);

      ctx.fillStyle = shareSurface(meta.host || document.body);
      ctx.fillRect(0, 0, W, H);
      ctx.strokeStyle = t.grid2;
      ctx.lineWidth = 1;
      ctx.strokeRect(0.5, 0.5, W - 1, H - 1);

      if (icon) ctx.drawImage(icon, PAD, 36, 36, 36);
      ctx.font = "600 15px " + t.mono;
      ctx.fillStyle = t.text0;
      ctx.fillText("P A L I M P S E S T", PAD + (icon ? 50 : 0), 59);
      ctx.font = "13px " + t.mono;
      ctx.fillStyle = t.text3;
      var dom = "palimpsest.info";
      ctx.fillText(dom, W - PAD - ctx.measureText(dom).width, 59);
      ctx.strokeStyle = t.grid;
      ctx.beginPath(); ctx.moveTo(PAD, 84.5); ctx.lineTo(W - PAD, 84.5); ctx.stroke();

      if (meta.kicker) {
        ctx.font = "13px " + t.mono;
        ctx.fillStyle = t.text4;
        ctx.fillText(meta.kicker.toUpperCase(), PAD, 132);
      }
      ctx.font = "650 30px " + t.mono;
      ctx.fillStyle = t.text0;
      shareWrap(ctx, meta.title, PAD, titleTop, W - PAD * 2, 42, 3, true);
      if (meta.body) {
        ctx.font = "15px " + t.mono;
        ctx.fillStyle = t.text2;
        shareWrap(ctx, meta.body, PAD, bodyTop, W - PAD * 2 - 8, 26, 5, true);
      }

      ctx.strokeStyle = t.grid;
      ctx.beginPath(); ctx.moveTo(PAD, ruleY + 0.5); ctx.lineTo(W - PAD, ruleY + 0.5); ctx.stroke();
      ctx.font = "13px " + t.mono;
      var f1 = ruleY + 28;
      ctx.fillStyle = t.text2;
      ctx.fillText(meta.link.replace(/^https?:\/\//, ""), PAD, f1);
      ctx.fillStyle = t.text4;
      var now = new Date();
      var m2 = function (n) { return String(n).padStart(2, "0"); };
      var ex = "exported " + now.getUTCDate() + " " +
        ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][now.getUTCMonth()] +
        " " + now.getUTCFullYear() + ", " + m2(now.getUTCHours()) + ":" + m2(now.getUTCMinutes()) + " UTC";
      ctx.fillText(ex, W - PAD - ctx.measureText(ex).width, f1);
      return cv;
    });
  }
  function initPageShare() {
    var path = location.pathname;
    var kicker =
      path.indexOf("/readings/") === 0 ? "reading" :
      path.indexOf("/china-brief") === 0 ? "china brief" :
      path.indexOf("/dashboards/") === 0 ? "dashboard" : null;
    if (!kicker) return;
    var main = document.querySelector("main") || document.body;
    var h1 = main.querySelector("h1");
    if (!h1) return;
    var descEl = document.querySelector('meta[name="description"]');
    var meta = {
      title: h1.textContent.replace(/\s+/g, " ").trim(),
      body: descEl ? descEl.getAttribute("content") : "",
      kicker: kicker,
      link: location.origin + location.pathname,
      host: h1
    };
    var compose = function () { return composePageCard(meta); };
    var toBlob = function (cv) {
      return new Promise(function (res, rej) {
        cv.toBlob(function (b) { b ? res(b) : rej(new Error("toBlob failed")); }, "image/png");
      });
    };
    var fileStamp = function () {
      var d = new Date();
      var p = function (n) { return String(n).padStart(2, "0"); };
      return "palimpsest-" +
        meta.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48) +
        "-" + d.getUTCFullYear() + p(d.getUTCMonth() + 1) + p(d.getUTCDate()) + ".png";
    };
    var download = function () {
      return compose().then(toBlob).then(function (blob) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = fileStamp();
        a.click();
        setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
      });
    };

    var row = document.createElement("div");
    row.className = "ps-share";
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", "share this page");
    var note = document.createElement("span");
    note.className = "ps-share__note";
    note.setAttribute("role", "status");
    var noteTimer = 0;
    var say = function (msg) {
      note.textContent = msg;
      clearTimeout(noteTimer);
      noteTimer = setTimeout(function () { note.textContent = ""; }, 2200);
    };
    var btn = function (label, fn, title) {
      if (row.childNodes.length) {
        var dot = document.createElement("span");
        dot.className = "ps-share__dot";
        dot.textContent = "·";
        row.appendChild(dot);
      }
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      if (title) b.title = title;
      b.addEventListener("click", fn);
      row.appendChild(b);
    };

    btn("share card", function () {
      /* Safari accepts a clipboard write only when the ClipboardItem is built
         synchronously in the gesture, with the blob as a promise. */
      var wrote = false;
      try {
        if (navigator.clipboard && typeof ClipboardItem !== "undefined") {
          navigator.clipboard.write([new ClipboardItem({ "image/png": compose().then(toBlob) })])
            .then(function () { say("card copied as image"); },
                  function () { download().then(function () { say("copy blocked · png saved"); },
                                               function () { say("export failed"); }); });
          wrote = true;
        }
      } catch (e) { /* fall through */ }
      if (!wrote) download().then(function () { say("png saved"); }, function () { say("export failed"); });
    }, "copy a social card for this page");
    btn("download png", function () {
      download().then(function () { say("png saved"); }, function () { say("export failed"); });
    }, "download the social card as a png");
    var shareable = false;
    try {
      shareable = !!(navigator.canShare && navigator.canShare({
        files: [new File([new Uint8Array(1)], "x.png", { type: "image/png" })]
      }));
    } catch (e) { shareable = false; }
    if (shareable) {
      btn("share", function () {
        compose().then(toBlob).then(function (blob) {
          return navigator.share({
            files: [new File([blob], fileStamp(), { type: "image/png" })],
            title: meta.title,
            text: meta.link
          });
        }).catch(function (e) { if (!e || e.name !== "AbortError") say("share failed"); });
      }, "share the card image");
    }
    btn("copy link", function () {
      navigator.clipboard.writeText(meta.link)
        .then(function () { say("link copied"); }, function () { say("copy blocked"); });
    }, "copy a link to this page");
    row.appendChild(note);
    h1.parentNode.insertBefore(row, h1.nextSibling);
  }

  function init() {
    initNav();
    initStagger();
    initReveal();
    initPageShare();
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
