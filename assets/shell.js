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
    var compactViewport = matchMedia("(max-width: 940px)");
    var isCompact = function () { return compactViewport.matches; };

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
        /* Moving from the trigger into an absolutely positioned flyout can
           cross a few pixels where the document, rather than the nav item, is
           the hit target. Entering the panel must therefore cancel the pending
           close explicitly; relying on the ancestor's pointerenter leaves the
           timer armed in Chromium and WebKit. */
        panel.addEventListener("pointerenter", function () {
          if (isCompact()) return;
          clearTimeout(openTimer);
          clearTimeout(closeTimer);
          open(item);
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
      var menu = nav.querySelector(".ps-nav__items");

      function mobileMenuFocusables() {
        if (!menu) return [burger];
        var controls = [].slice.call(menu.querySelectorAll(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
        ));
        controls = controls.filter(function (control) {
          var flyout = control.closest(".ps-flyout");
          return !flyout || control.closest(".ps-nav__item").hasAttribute("data-open");
        });
        controls.push(burger);
        return controls;
      }

      function setMobileMenu(openNow, restoreFocus) {
        if (openNow) {
          document.body.setAttribute("data-ps-menu", "");
          burger.setAttribute("aria-expanded", "true");
          requestAnimationFrame(function () {
            if (!document.body.hasAttribute("data-ps-menu")) return;
            var firstLink = menu && menu.querySelector(
              '.ps-nav__item > a[href], .ps-nav__item > button:not([disabled])'
            );
            if (firstLink) firstLink.focus();
          });
          return;
        }
        document.body.removeAttribute("data-ps-menu");
        burger.setAttribute("aria-expanded", "false");
        closeAll(null);
        if (restoreFocus !== false) burger.focus();
      }

      burger.addEventListener("click", function () {
        setMobileMenu(!document.body.hasAttribute("data-ps-menu"));
      });
      if (menu) {
        menu.addEventListener("click", function (e) {
          if (!document.body.hasAttribute("data-ps-menu") || !e.target.closest) return;
          var link = e.target.closest("a[href]");
          if (!link || !menu.contains(link)) return;
          var href = link.getAttribute("href");
          if (!href) return;
          var internal = href.charAt(0) === "#";
          if (!internal) {
            try {
              internal = new URL(href, window.location.href).origin === window.location.origin;
            } catch (err) {
              internal = false;
            }
          }
          if (internal) setMobileMenu(false, false);
        });
      }
      var scrim = nav.querySelector(".ps-nav__scrim");
      if (scrim) scrim.addEventListener("click", function () { setMobileMenu(false); });
      var resetMobileSheet = function () {
        if (!compactViewport.matches && document.body.hasAttribute("data-ps-menu")) {
          setMobileMenu(false, false);
        }
      };
      if (compactViewport.addEventListener) {
        compactViewport.addEventListener("change", resetMobileSheet);
      } else if (compactViewport.addListener) {
        compactViewport.addListener(resetMobileSheet);
      }
      addEventListener("keydown", function (e) {
        if (!document.body.hasAttribute("data-ps-menu")) return;
        if (e.key === "Escape") {
          e.preventDefault();
          setMobileMenu(false);
          return;
        }
        if (e.key !== "Tab") return;

        var focusables = mobileMenuFocusables();
        var first = focusables[0], last = focusables[focusables.length - 1];
        if (e.shiftKey && (document.activeElement === first || !nav.contains(document.activeElement))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
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

  /* ----------------------------------------- bounded growth evidence ---- */
  /* Cloudflare Web Analytics supplies page-level discovery. This endpoint
     adds only named funnel actions. It deliberately omits cookies, user IDs,
     raw referrers, client clocks and free-form fields. A browser event is
     activity evidence, never proof of a distinct person or subscriber. */
  var GROWTH_SCHEMA = "palimpsest.growth-event.v1";
  var GROWTH_HOST = "www.palimpsest.info";
  var GROWTH_EVENTS = {
    brief_clicked: true,
    citation_copied: true,
    deep_read: true,
    download_started: true,
    feed_clicked: true,
    follow_clicked: true,
    inquiry_clicked: true,
    private_diligence_clicked: true,
    weekly_brief_clicked: true
  };

  function growthDisabled() {
    if (location.hostname !== GROWTH_HOST) return true;
    if (navigator.doNotTrack === "1" || window.doNotTrack === "1") return true;
    if (navigator.globalPrivacyControl === true) return true;
    try {
      return localStorage.getItem("palimpsest_analytics_opt_out") === "1";
    } catch (error) {
      return false;
    }
  }

  function growthPage() {
    var path = location.pathname;
    if (path === "/") return path;
    if (path === "/news/") return path;
    if (/^\/news\/china\/situation\/(?:page\/[1-9][0-9]*\/)?$/.test(path)) return path;
    return null;
  }

  function growthSource() {
    if (!document.referrer) return "direct";
    try {
      var host = new URL(document.referrer).hostname.toLowerCase();
      if (host === location.hostname.toLowerCase()) return "same_site";
      if (/(^|\.)(chatgpt|openai|perplexity|gemini|claude|copilot)\./.test(host)) return "ai";
      if (/(^|\.)(google|bing|duckduckgo|yahoo|baidu|yandex)\./.test(host)) return "search";
      if (host === "t.me" || /(^|\.)(telegram|facebook|linkedin|twitter|x)\./.test(host)) return "social";
    } catch (error) {
      return "other";
    }
    return "other";
  }

  function trackGrowth(eventName, locationName) {
    var page = growthPage();
    if (growthDisabled() || !page || !GROWTH_EVENTS[eventName]) return false;
    var body = JSON.stringify({
      schema_version: GROWTH_SCHEMA,
      event: eventName,
      location: locationName,
      page: page,
      source: growthSource()
    });
    if (navigator.sendBeacon) {
      return navigator.sendBeacon(
        "/events",
        new Blob([body], { type: "application/json" })
      );
    }
    if (window.fetch) {
      fetch("/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body,
        credentials: "same-origin",
        keepalive: true
      }).catch(function () {});
      return true;
    }
    return false;
  }

  function initGrowthEvidence() {
    if (growthDisabled() || !document.body.hasAttribute("data-growth-page")) return;
    document.addEventListener("click", function (event) {
      var target = event.target && event.target.closest
        ? event.target.closest("[data-growth-event]")
        : null;
      if (!target) return;
      trackGrowth(
        target.getAttribute("data-growth-event"),
        target.getAttribute("data-growth-location")
      );
    });

    var visibleSince = document.hidden ? null : Date.now();
    var visibleMilliseconds = 0;
    var sentDeepRead = false;
    function settleVisibility() {
      if (visibleSince !== null) {
        visibleMilliseconds += Date.now() - visibleSince;
        visibleSince = null;
      }
    }
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) settleVisibility();
      else visibleSince = Date.now();
    });
    window.setInterval(function () {
      if (sentDeepRead || document.hidden) return;
      var documentHeight = Math.max(
        document.documentElement.scrollHeight,
        document.body.scrollHeight
      );
      var depth = (window.scrollY + window.innerHeight) / Math.max(documentHeight, 1);
      var elapsed = visibleMilliseconds + (visibleSince === null ? 0 : Date.now() - visibleSince);
      if (depth >= 0.5 && elapsed >= 20000) {
        sentDeepRead = trackGrowth(
          "deep_read",
          document.body.getAttribute("data-growth-page")
        );
      }
    }, 1000);
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
      path.indexOf("/china/") === 0 ? "china observatory" :
      path.indexOf("/osint-china") === 0 ? "osint china" :
      path.indexOf("/china-brief") === 0 ? "china brief" :
      path.indexOf("/dashboards/") === 0 ? "dashboard" :
      path.indexOf("/for-researchers") === 0 ? "research guide" :
      path.indexOf("/guides/") === 0 ? "public guide" :
      path.indexOf("/news/") === 0 ? "newsroom" : null;
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
        trackGrowth("download_started", "page_share");
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
    btn("copy citation", function () {
      var accessed = new Date().toISOString().slice(0, 10);
      var citation = 'Palimpsest. "' + meta.title + '." Palimpsest, accessed ' +
        accessed + ". " + meta.link;
      navigator.clipboard.writeText(citation)
        .then(function () {
          say("citation copied");
          trackGrowth("citation_copied", "page_share");
        }, function () { say("copy blocked"); });
    }, "copy a citation with the canonical URL and access date");
    row.appendChild(note);
    h1.parentNode.insertBefore(row, h1.nextSibling);
  }

  /* -------------------------------------------------------- card share ---- */
  /* Every signal card can leave the site on its own: kicker, title, the big
     number, its caption and the honest body line, under the brand row. One
     chip, app-like: the OS share sheet where it exists, the clipboard where
     it does not, a download as the last resort. */
  function cleanText(el) {
    return el ? String(el.innerText || el.textContent || "").replace(/\s+/g, " ").trim() : "";
  }
  function harvestCard(card) {
    var pick = function (sel) { return cleanText(card.querySelector(sel)); };
    var title = pick("h1, h2, h3, h4");
    var kicker = pick("[data-share-kicker], .sig__k, .lead__k, .note__h, .ipi-read__k");
    /* the big number: the largest short digit-bearing leaf in the card */
    var valueEl = null, best = 0;
    card.querySelectorAll("*").forEach(function (el) {
      if (el.childElementCount > 1) return;
      var txt = cleanText(el);
      if (!txt || txt.length > 24 || !/\d/.test(txt)) return;
      var size = parseFloat(getComputedStyle(el).fontSize) || 0;
      if (size >= 24 && size > best) { best = size; valueEl = el; }
    });
    var value = cleanText(valueEl), caption = "";
    if (valueEl) {
      var block = valueEl.closest("p, div") || valueEl.parentElement;
      if (block && block !== card) {
        caption = cleanText(block).replace(value, " ").replace(/\s+/g, " ").trim();
      }
      /* a caption that opens with a bare unit belongs on the number itself */
      var um = caption.match(/^([%‰]|\/\d+[a-z%]*)\s*(.*)$/);
      if (um) { value += um[1]; caption = um[2]; }
    }
    var body = pick("[data-share-body], .sig__b, .lead__b");
    var meta = pick("[data-share-meta], .sig__f, .fresh");
    if (!title) { title = kicker; kicker = cleanText(document.querySelector("main h1")); }
    var link = card.href
      ? card.href
      : location.origin + location.pathname + (card.id ? "#" + card.id : location.hash);
    return { title: title, kicker: kicker, value: value, caption: caption,
             body: body, meta: meta, link: link, host: card };
  }
  function composeSignalCard(m) {
    var W = 1200, PAD = 48;
    return shareIcon().then(function (icon) {
      var t = shareTokens();
      var probe = document.createElement("canvas").getContext("2d");

      probe.font = "650 32px " + t.mono;
      var titleTop = 172;
      var y = shareWrap(probe, m.title, PAD, titleTop, W - PAD * 2, 44, 2, false);
      var valueY = 0, capTop = 0;
      if (m.value) {
        valueY = y + 118;
        y = valueY;
        if (m.caption) {
          probe.font = "16px " + t.mono;
          capTop = y + 36;
          y = shareWrap(probe, m.caption, PAD, capTop, W - PAD * 2 - 8, 27, 2, false);
        }
      }
      var bodyTop = 0;
      if (m.body) {
        probe.font = "15px " + t.mono;
        bodyTop = y + 40;
        y = shareWrap(probe, m.body, PAD, bodyTop, W - PAD * 2 - 8, 26, 4, false);
      }
      var H = Math.max(y + 110, 520);
      var ruleY = H - 68;

      var EX = 2;
      var cv = document.createElement("canvas");
      cv.width = W * EX;
      cv.height = H * EX;
      var ctx = cv.getContext("2d");
      ctx.scale(EX, EX);

      ctx.fillStyle = shareSurface(m.host || document.body);
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

      if (m.kicker) {
        ctx.font = "13px " + t.mono;
        ctx.fillStyle = t.text4;
        ctx.fillText(m.kicker.toUpperCase(), PAD, 130);
      }
      ctx.font = "650 32px " + t.mono;
      ctx.fillStyle = t.text0;
      shareWrap(ctx, m.title, PAD, titleTop, W - PAD * 2, 44, 2, true);
      if (m.value) {
        ctx.font = "800 92px " + t.mono;
        ctx.fillStyle = t.text0;
        ctx.fillText(m.value, PAD, valueY);
        if (m.caption) {
          ctx.font = "16px " + t.mono;
          ctx.fillStyle = t.text2;
          shareWrap(ctx, m.caption, PAD, capTop, W - PAD * 2 - 8, 27, 2, true);
        }
      }
      if (m.body) {
        ctx.font = "15px " + t.mono;
        ctx.fillStyle = t.text2;
        shareWrap(ctx, m.body, PAD, bodyTop, W - PAD * 2 - 8, 26, 4, true);
      }

      ctx.strokeStyle = t.grid;
      ctx.beginPath(); ctx.moveTo(PAD, ruleY + 0.5); ctx.lineTo(W - PAD, ruleY + 0.5); ctx.stroke();
      ctx.font = "13px " + t.mono;
      var f1 = ruleY + 28;
      ctx.fillStyle = t.text2;
      ctx.fillText(m.link.replace(/^https?:\/\//, ""), PAD, f1);
      ctx.fillStyle = t.text4;
      var now = new Date();
      var p2 = function (n) { return String(n).padStart(2, "0"); };
      var right = (m.meta ? m.meta + " · " : "") + "exported " + now.getUTCDate() + " " +
        ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][now.getUTCMonth()] +
        " " + now.getUTCFullYear() + ", " + p2(now.getUTCHours()) + ":" + p2(now.getUTCMinutes()) + " UTC";
      ctx.fillText(right, W - PAD - ctx.measureText(right).width, f1);
      return cv;
    });
  }
  function initCardShare() {
    var cards = document.querySelectorAll("a.door, a.lead, a.sig, [data-share-card]");
    cards.forEach(function (card) {
      if (card.querySelector(".ps-cardshare")) return;
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "ps-cardshare";
      chip.textContent = "share";
      chip.title = "share this card as an image";
      var chipTimer = 0;
      var say = function (msg) {
        chip.textContent = msg;
        clearTimeout(chipTimer);
        chipTimer = setTimeout(function () { chip.textContent = "share"; }, 2000);
      };
      chip.addEventListener("click", function (e) {
        /* the card itself is a link; the chip must not navigate */
        e.preventDefault();
        e.stopPropagation();
        var m = harvestCard(card);
        if (!m.title) { say("nothing to share"); return; }
        var toBlob = function (cv) {
          return new Promise(function (res, rej) {
            cv.toBlob(function (b) { b ? res(b) : rej(new Error("toBlob failed")); }, "image/png");
          });
        };
        var name = "palimpsest-" +
          m.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48) + ".png";
        var download = function () {
          return composeSignalCard(m).then(toBlob).then(function (blob) {
            var url = URL.createObjectURL(blob);
            var a = document.createElement("a");
            a.href = url;
            a.download = name;
            a.click();
            setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
          });
        };
        var shareable = false;
        try {
          shareable = !!(navigator.canShare && navigator.canShare({
            files: [new File([new Uint8Array(1)], "x.png", { type: "image/png" })]
          }));
        } catch (err) { shareable = false; }
        if (shareable) {
          composeSignalCard(m).then(toBlob).then(function (blob) {
            return navigator.share({
              files: [new File([blob], name, { type: "image/png" })],
              title: m.title,
              text: m.link
            });
          }).catch(function (err) { if (!err || err.name !== "AbortError") say("share failed"); });
          return;
        }
        var wrote = false;
        try {
          if (navigator.clipboard && typeof ClipboardItem !== "undefined") {
            navigator.clipboard.write([new ClipboardItem({ "image/png": composeSignalCard(m).then(toBlob) })])
              .then(function () { say("copied ✓"); },
                    function () { download().then(function () { say("saved ✓"); },
                                                 function () { say("failed"); }); });
            wrote = true;
          }
        } catch (err) { /* fall through */ }
        if (!wrote) download().then(function () { say("saved ✓"); }, function () { say("failed"); });
      });
      card.appendChild(chip);
    });
  }

  /* Privacy-first portfolio measurement. Cloudflare's beacon is loaded from
     the shared shell so the site's hundreds of static pages stay in sync. */
  function initWebAnalytics() {
    if (document.querySelector("script[data-cf-beacon]")) return;
    var beacon = document.createElement("script");
    beacon.type = "module";
    beacon.src = "https://static.cloudflareinsights.com/beacon.min.js";
    beacon.setAttribute("data-cf-beacon", '{"token":"99a9c5f167624ed488a68a34b5513371"}');
    document.head.appendChild(beacon);
  }

  /* ---------------------------------------------------- freshness lease ---- */
  /* GitHub Pages can publish a new edition while a reader keeps an old tab
     open indefinitely. Renew that tab after one hour, but never destroy text a
     reader is entering into a form. Returning to a backgrounded tab also
     performs the check immediately instead of waiting for the next timer tick. */
  function initFreshnessLease() {
    var loadedAt = Date.now();
    var leaseMs = 60 * 60 * 1000;
    var formIsDirty = false;

    document.addEventListener("input", function (event) {
      if (event.target && event.target.closest && event.target.closest("form")) {
        formIsDirty = true;
      }
    });

    function renewIfDue() {
      if (document.hidden || formIsDirty || Date.now() - loadedAt < leaseMs) return;
      window.location.reload();
    }

    window.setInterval(renewIfDue, 60 * 1000);
    document.addEventListener("visibilitychange", renewIfDue);
  }

  function init() {
    initNav();
    initStagger();
    initReveal();
    initPageShare();
    initCardShare();
    initWebAnalytics();
    initGrowthEvidence();
    initFreshnessLease();
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
    trackGrowth: trackGrowth,
    reducedMotion: RM
  };
})();
