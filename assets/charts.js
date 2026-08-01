/* ============================================================================
   PALIMPSEST CHARTS
   ----------------------------------------------------------------------------
   Every chart on this site obeys the same contract as the live figures:

     * it draws only from a published file, fetched when the page loads;
     * it draws only when the data actually supports drawing — below the
       minimum, the reader gets a plain sentence with the honest count, and
       the raw file link, never an interpolated guess;
     * a gap in the record is drawn as a gap. Nothing joins across missing
       runs, nothing is smoothed, and a stale series says so in words;
     * every chart has a table-view twin carrying the exact values, so the
       tooltip enhances and never gates;
     * series colour never carries meaning alone — legends, direct labels,
       glyphs and words do the identity work everywhere it matters.

   No dependencies. Tokens come from tikto.css / charts.css at runtime.
   ========================================================================== */
(function () {
  "use strict";

  const RM = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

  /* ------------------------------------------------------------- tokens ---- */
  let T = null;
  function cssVar(name, fb) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fb;
  }
  function tokens() {
    if (T) return T;
    T = {
      text0: cssVar("--tk-text-0", "#ffffff"),
      text1: cssVar("--tk-text-1", "#e2e8f0"),
      text2: cssVar("--tk-text-2", "#94a3b8"),
      text3: cssVar("--tk-text-3", "#7d8ca3"),
      text4: cssVar("--tk-text-4", "#6f8098"),
      grid:  cssVar("--tk-line-1", "#1a1a1a"),
      grid2: cssVar("--tk-line-2", "#272727"),
      live:  cssVar("--tk-live", "#06d6e0"),
      ok:    cssVar("--tk-ok", "#19c393"),
      warn:  cssVar("--tk-warning", "#ffb020"),
      watch: cssVar("--tk-watch", "#4aa3ff"),
      crit:  cssVar("--tk-critical", "#ff5b52"),
      stale: cssVar("--tk-stale", "#8593a8"),
      cat1:  cssVar("--pc-cat-1", "#12a7b8"),
      cat2:  cssVar("--pc-cat-2", "#8259ea"),
      ramp: [cssVar("--pc-ramp-0", "#7a2f2a"), cssVar("--pc-ramp-1", "#a63c35"),
             cssVar("--pc-ramp-2", "#d4544a"), cssVar("--pc-ramp-3", "#ff8f85")],
      mono: cssVar("--tk-font-mono", "ui-monospace, Menlo, monospace")
    };
    return T;
  }
  /* The surface behind a mark, for the 2px ring that keeps overlapping marks
     legible. Walk up until something actually paints. */
  function surfaceOf(el) {
    let n = el;
    while (n && n !== document.documentElement) {
      const bg = getComputedStyle(n).backgroundColor;
      if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") return bg;
      n = n.parentElement;
    }
    return "#0e0e0e";
  }

  function hexRgb(hex) {
    let h = hex.replace("#", "").trim();
    if (h.length === 3) h = h.split("").map(c => c + c).join("");
    const v = parseInt(h, 16);
    return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
  }
  function rgba(color, a) {
    if (color.startsWith("#")) {
      const c = hexRgb(color);
      return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")";
    }
    return color; /* rgb()/var-resolved strings pass through at full alpha */
  }
  function lerpHex(h1, h2, p) {
    const a = hexRgb(h1), b = hexRgb(h2);
    const m = a.map((v, i) => Math.round(v + (b[i] - v) * p));
    return "rgb(" + m[0] + "," + m[1] + "," + m[2] + ")";
  }
  function rampColor(share) {
    const R = tokens().ramp;
    const x = Math.max(0, Math.min(1, share)) * (R.length - 1);
    const i = Math.min(R.length - 2, Math.floor(x));
    return lerpHex(R[i], R[i + 1], x - i);
  }

  /* -------------------------------------------------------------- utils ---- */
  function num(v) { return (typeof v === "number" && isFinite(v)) ? v : null; }
  function pick(obj, path) {
    let cur = obj;
    for (const k of path.split(".")) {
      if (cur == null || typeof cur !== "object") return null;
      cur = cur[k];
    }
    return cur;
  }
  function fmtVal(v, dec, unit) {
    if (v == null) return "—";
    return v.toFixed(dec == null ? 1 : dec) + (unit || "");
  }
  function pad2(n) { return String(n).padStart(2, "0"); }
  function fmtD(t) {
    const d = new Date(t);
    return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()];
  }
  function fmtDT(t) {
    const d = new Date(t);
    return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + " " +
           pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes()) + " UTC";
  }
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function niceTicks(min, max, want) {
    if (!(max > min)) { max = min + 1; }
    const span = max - min;
    const step0 = Math.pow(10, Math.floor(Math.log10(span / want)));
    let step = step0;
    for (const m of [1, 2, 2.5, 5, 10]) {
      if (span / (step0 * m) <= want) { step = step0 * m; break; }
    }
    const out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) {
      out.push(Math.abs(v) < 1e-9 ? 0 : +v.toFixed(6));
    }
    return out;
  }

  /* ----------------------------------------------------------- fetching ---- */
  /* When a history row was written. Signals that log a heartbeat carry
     generated_at; signals keyed by the DATA date (one row per trading day or
     per release) carry `date` instead and deliberately have no run stamp.
     Reading only generated_at silently DISCARDED every row of those files, so
     a chart pointed at them could never draw and would fail as though the
     feed were empty. This is the same precedence the board applies in
     processors/conformal_events._row_timestamp, so both consumers of a
     history file now agree on when its rows happened. */
  function rowTime(o) {
    for (const k of ["generated_at", "as_of", "at", "date"]) {
      const v = o[k];
      if (typeof v === "string" && v) {
        const t = Date.parse(v);
        if (isFinite(t)) return t;
      }
    }
    return NaN;
  }

  const CACHE = {};
  function loadHistory(url) {
    if (CACHE[url]) return CACHE[url];
    CACHE[url] = fetch(url, { cache: "no-store" })
      .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); })
      .then(txt => {
        const rows = [];
        for (const line of txt.split("\n")) {
          const s = line.trim();
          if (!s) continue;
          try {
            const o = JSON.parse(s);
            if (o && typeof o === "object" && isFinite(rowTime(o))) rows.push(o);
          } catch (e) { /* a torn line is skipped, never guessed at */ }
        }
        rows.sort((a, b) => rowTime(a) - rowTime(b));
        return rows;
      });
    return CACHE[url];
  }
  function loadJson(url) {
    return fetch(url, { cache: "no-store" })
      .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }

  /* --------------------------------------------------- honest fallbacks ---- */
  function fallbackEl(host) {
    return host.querySelector(".pc__fallback");
  }
  function failNote(host, url, msg) {
    if (host.querySelector(".pc__err")) return;
    const e = el("span", "pc__err", (msg || "live history unavailable") + " · ");
    const a = el("a", null, "raw file");
    a.href = url;
    e.appendChild(a);
    (fallbackEl(host) || host).appendChild(e);
  }
  function shortNote(host, url, n, min) {
    const fb = fallbackEl(host);
    if (!fb) return;
    fb.textContent = n + " reading" + (n === 1 ? "" : "s") + " on record. This " +
      "chart draws itself once there are " + min + "; until then the count is " +
      "the finding, and every reading is already published in the raw file. ";
    const a = el("a", null, "raw JSONL");
    a.href = url;
    fb.appendChild(a);
  }

  /* ------------------------------------------------------------ tooltip ---- */
  function makeTip(wrap) {
    const tip = el("div", "pc__tip");
    tip.setAttribute("aria-hidden", "true");
    wrap.appendChild(tip);
    return {
      node: tip,
      show(x, y, title, rows) {
        tip.textContent = "";
        tip.appendChild(el("p", "pc__tip-t", title));
        for (const r of rows) {
          const line = el("div", "pc__tip-r");
          if (r.color) {
            const k = el("i");
            k.style.background = r.color;
            line.appendChild(k);
          }
          line.appendChild(el("b", null, r.value));
          if (r.label) line.appendChild(el("span", null, r.label));
          tip.appendChild(line);
        }
        tip.classList.add("is-on");
        const W = wrap.clientWidth, tw = tip.offsetWidth, th = tip.offsetHeight;
        let tx = x + 14, ty = y - th - 10;
        if (tx + tw > W - 4) tx = x - tw - 14;
        if (tx < 4) tx = 4;
        if (ty < 4) ty = y + 16;
        tip.style.left = tx + "px";
        tip.style.top = ty + "px";
      },
      hide() { tip.classList.remove("is-on"); }
    };
  }

  /* A horizontally scrolling box that a KEYBOARD can also scroll. Touch pans
     it for free; without tabindex nobody who cannot use a pointer can reach
     the right-hand columns, and a screen reader gets no region to announce.
     The house pattern in the hand-written pages is the same three attributes,
     so generated boxes carry them too. */
  function scrollBox(cls, label) {
    const box = el("div", cls || "ps-scroll-x");
    box.tabIndex = 0;
    box.setAttribute("role", "region");
    box.setAttribute("aria-label", (label || "table view") + ", scrolls horizontally");
    return box;
  }

  /* --------------------------------------------------------- table twin ---- */
  function tableTwin(host, headers, rows, summary) {
    const det = el("details", "pc-table");
    det.appendChild(el("summary", null, summary || "table view"));
    const scroll = scrollBox("ps-scroll-x", summary || "table view");
    const table = el("table");
    const thead = el("thead"), trh = el("tr");
    headers.forEach((h, i) => {
      const th = el("th", i > 0 ? "n" : null, h);
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const r of rows) {
      const tr = el("tr");
      r.forEach((c, i) => tr.appendChild(el("td", i > 0 ? "n" : null, c)));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    scroll.appendChild(table);
    det.appendChild(scroll);
    host.appendChild(det);
    return det;
  }

  /* --------------------------------------------------------- canvas kit ---- */
  function sizeCanvas(cv, cssH) {
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const w = cv.clientWidth || cv.parentElement.clientWidth || 600;
    const bw = Math.round(w * dpr), bh = Math.round(cssH * dpr);
    cv.style.height = cssH + "px";
    // Assigning width/height reallocates the backing store and clears it, and
    // draw() runs on every pointermove. At DPR 3 that is a megapixel buffer
    // thrown away per frame — the scrub visibly stutters on a mid-range phone.
    // Only resize when the size actually changed; the transform is reset
    // unconditionally because callers rely on a clean matrix either way.
    if (cv.width !== bw) cv.width = bw;
    if (cv.height !== bh) cv.height = bh;
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h: cssH };
  }
  function reveal(draw, done) {
    if (RM) { draw(1); if (done) done(); return; }
    const t0 = performance.now(), D = 650;
    requestAnimationFrame(function f(t) {
      const p = Math.min(1, (t - t0) / D);
      draw(1 - Math.pow(1 - p, 3));
      if (p < 1) requestAnimationFrame(f);
      else if (done) done();
    });
  }

  /* ============================================================ TREND ======
     cfg = {
       url | rows,        published JSONL (or pre-filtered rows)
       series: [{key, label, color?, dec?}],   1 or 2 — more becomes multiples
       unit,              suffix on values ("%", "/100")
       dec,               default decimals
       h,                 css height (default 200)
       minPoints,         below this the sentence stands (default 8)
       zero,              include 0 in the domain and draw the area wash
       staleHours,        past this the axis row says stale, in words
       gapFactor,         dt > gapFactor × median dt breaks the line (2.6)
       markers: [{iso, label, kind: watch|alarm|note}],
       ariaTitle,         name for the accessible summary
     }
     ======================================================================== */
  function trend(host, cfg) {
    const t = tokens();
    const colors = [cfg.series[0].color || t.cat1,
                    (cfg.series[1] && cfg.series[1].color) || t.cat2];
    const source = cfg.rows ? Promise.resolve(cfg.rows) : loadHistory(cfg.url);

    source.then(rows => {
      const series = cfg.series.map((s, i) => ({
        label: s.label, dec: s.dec != null ? s.dec : cfg.dec,
        color: colors[i],
        pts: rows.map(r => ({ t: rowTime(r), v: num(pick(r, s.key)) }))
                 .filter(p => isFinite(p.t))
      }));
      const master = series[0].pts.filter(p => p.v != null);
      const min = cfg.minPoints || 8;
      if (master.length < min) { shortNote(host, cfg.url, master.length, min); return; }
      mount(host, cfg, series, t);
    }).catch(() => failNote(host, cfg.url));
  }

  function mount(host, cfg, series, t) {
    /* the chart takes the fallback sentence's place; static captions after it
       keep their position, and the table twin lands at the very end */
    const fb = fallbackEl(host);
    const put = n => host.insertBefore(n, fb || null);

    /* legend: always present for two series, absent for one */
    if (series.length > 1) {
      const lg = el("div", "pc__legend");
      for (const s of series) {
        const item = el("span", null, s.label);
        const k = el("i");
        k.style.background = s.color;
        item.prepend(k);
        lg.appendChild(item);
      }
      put(lg);
    }

    const wrap = el("div", "pc__wrap");
    const cv = el("canvas", "pc__cv");
    cv.setAttribute("tabindex", "0");
    cv.setAttribute("role", "img");
    wrap.appendChild(cv);
    put(wrap);
    const tip = makeTip(wrap);

    const axis = el("div", "pc__axis");
    put(axis);
    if (fb) fb.remove();

    /* ---- the data, shaped for drawing ---- */
    const times = series[0].pts.map(p => p.t);
    const t0 = times[0], t1 = times[times.length - 1];
    const dts = [];
    for (let i = 1; i < times.length; i++) dts.push(times[i] - times[i - 1]);
    dts.sort((a, b) => a - b);
    const medDt = dts.length ? dts[Math.floor(dts.length / 2)] : 1;
    const gapAt = (cfg.gapFactor || 2.6) * medDt;

    let vmin = Infinity, vmax = -Infinity;
    for (const s of series) for (const p of s.pts) {
      if (p.v == null) continue;
      if (p.v < vmin) vmin = p.v;
      if (p.v > vmax) vmax = p.v;
    }
    if (cfg.zero) vmin = Math.min(0, vmin);
    const spanPad = (vmax - vmin || 1) * 0.09;
    let y0 = cfg.zero && vmin === 0 ? 0 : vmin - spanPad;
    let y1 = vmax + spanPad;
    const yTicks = niceTicks(y0, y1, 3);

    /* stale is said in words on the axis row, never colour alone */
    const last = times[times.length - 1];
    const stale = cfg.staleHours && (Date.now() - last) > cfg.staleHours * 3600e3;
    axis.appendChild(el("span", null, fmtD(t0)));
    const mid = el("span", null, fmtD(t0 + (t1 - t0) / 2));
    axis.appendChild(mid);
    axis.appendChild(el("span", stale ? "is-stale" : null,
      stale ? "stale · last " + fmtD(t1) : fmtD(t1)));

    /* markers parsed once */
    const markers = (cfg.markers || [])
      .map(m => ({ t: Date.parse(m.iso), label: m.label, kind: m.kind || "note" }))
      .filter(m => isFinite(m.t) && m.t >= t0 && m.t <= t1);

    const surface = surfaceOf(host);
    let hoverIdx = -1;
    let geom = null;

    function draw(prog) {
      const H = cfg.h || 200;
      const { ctx, w, h } = sizeCanvas(cv, H);
      ctx.clearRect(0, 0, w, h);
      ctx.font = "9.5px " + t.mono;

      /* measure the y labels so the plot claims the rest */
      let labW = 0;
      for (const v of yTicks) labW = Math.max(labW, ctx.measureText(fmtVal(v, cfg.dec != null && cfg.dec < 1 ? cfg.dec : 0, "")).width);
      const padL = Math.ceil(labW) + 10;
      /* each marker label gets its own row above the plot, so close dates
         cannot smear their labels into each other */
      const padR = 58, padT = markers.length ? 10 + markers.length * 11 : 10, padB = 6;
      const pw = w - padL - padR, ph = h - padT - padB;
      const X = tv => padL + ((tv - t0) / Math.max(1, t1 - t0)) * pw;
      const Y = v => padT + (1 - (v - yTicks[0]) / Math.max(1e-9, yTicks[yTicks.length - 1] - yTicks[0])) * ph;
      geom = { X, Y, padL, padT, pw, ph };

      /* grid: solid hairlines, one step off the surface, recessive */
      ctx.strokeStyle = t.grid;
      ctx.lineWidth = 1;
      ctx.fillStyle = t.text4;
      for (const v of yTicks) {
        const y = Math.round(Y(v)) + 0.5;
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + pw, y); ctx.stroke();
        ctx.fillText(fmtVal(v, cfg.dec != null && cfg.dec < 1 ? cfg.dec : 0, ""), 4, y + 3);
      }

      /* reveal clip */
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, padL + pw * prog + padR * prog, h);
      ctx.clip();

      /* markers under the data, one label row each */
      markers.forEach((m, mi) => {
        const x = Math.round(X(m.t)) + 0.5;
        const col = m.kind === "alarm" ? t.crit : m.kind === "watch" ? t.watch : t.text4;
        const ly = 8 + mi * 11;
        ctx.strokeStyle = rgba(col, 0.55);
        ctx.beginPath(); ctx.moveTo(x, ly + 3); ctx.lineTo(x, padT + ph); ctx.stroke();
        ctx.fillStyle = col;
        ctx.font = "9px " + t.mono;
        const glyph = (m.kind === "alarm" ? "▲ " : m.kind === "watch" ? "◆ " : "") + m.label;
        const tw = ctx.measureText(glyph).width;
        const lx = x + 5 + tw > padL + pw + padR ? x - tw - 5 : x + 5;
        ctx.fillText(glyph, lx, ly + 3);
      });

      /* each series: wash (only against a true zero baseline), 2px line, gaps */
      series.forEach(s => {
        const segs = [];
        let cur = [];
        let prev = null;
        for (const p of s.pts) {
          if (p.v == null) { if (cur.length) segs.push(cur); cur = []; prev = null; continue; }
          if (prev != null && (p.t - prev) > gapAt) { if (cur.length) segs.push(cur); cur = []; }
          cur.push(p); prev = p.t;
        }
        if (cur.length) segs.push(cur);

        for (const seg of segs) {
          if (cfg.zero && yTicks[0] === 0 && seg.length > 1) {
            ctx.beginPath();
            ctx.moveTo(X(seg[0].t), Y(0));
            for (const p of seg) ctx.lineTo(X(p.t), Y(p.v));
            ctx.lineTo(X(seg[seg.length - 1].t), Y(0));
            ctx.closePath();
            ctx.fillStyle = rgba(s.color, 0.10);
            ctx.fill();
          }
          ctx.beginPath();
          seg.forEach((p, i) => { const x = X(p.t), y = Y(p.v); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
          ctx.strokeStyle = s.color;
          ctx.lineWidth = 2;
          ctx.lineJoin = "round";
          ctx.lineCap = "round";
          ctx.stroke();
          if (seg.length === 1) {
            /* an isolated reading is a dot, not an invisible line */
            ctx.beginPath(); ctx.arc(X(seg[0].t), Y(seg[0].v), 2.5, 0, 7);
            ctx.fillStyle = s.color; ctx.fill();
          }
        }
      });

      /* end treatment: marker dot + selective direct label (the latest value) */
      const ends = [];
      series.forEach(s => {
        const live = s.pts.filter(p => p.v != null);
        if (!live.length) return;
        const e = live[live.length - 1];
        ends.push({ s, x: X(e.t), y: Y(e.v), v: e.v });
      });
      ends.sort((a, b) => a.y - b.y);
      for (let i = 1; i < ends.length; i++) {
        if (ends[i].y - ends[i - 1].y < 13) ends[i].ly = (ends[i - 1].ly || ends[i - 1].y) + 13;
      }
      for (const e of ends) {
        ctx.beginPath(); ctx.arc(e.x, e.y, 4, 0, 7);
        ctx.fillStyle = e.s.color; ctx.fill();
        ctx.lineWidth = 2; ctx.strokeStyle = surface; ctx.stroke();
        ctx.font = "10.5px " + t.mono;
        ctx.fillStyle = t.text1;
        ctx.fillText(fmtVal(e.v, e.s.dec, cfg.unit), e.x + 8, (e.ly || e.y) + 3.5);
      }

      /* crosshair */
      if (hoverIdx >= 0 && hoverIdx < times.length) {
        const hx = Math.round(X(times[hoverIdx])) + 0.5;
        ctx.strokeStyle = t.grid2;
        ctx.beginPath(); ctx.moveTo(hx, padT); ctx.lineTo(hx, padT + ph); ctx.stroke();
        series.forEach(s => {
          const p = s.pts[hoverIdx];
          if (!p || p.v == null) return;
          ctx.beginPath(); ctx.arc(X(p.t), Y(p.v), 4, 0, 7);
          ctx.fillStyle = s.color; ctx.fill();
          ctx.lineWidth = 2; ctx.strokeStyle = surface; ctx.stroke();
        });
      }
      ctx.restore();
    }

    /* accessible summary + the exact values twin */
    const latest = series[0].pts.filter(p => p.v != null).pop();
    cv.setAttribute("aria-label",
      (cfg.ariaTitle || series[0].label) + ": " + series[0].pts.length +
      " readings from " + fmtD(t0) + " to " + fmtD(t1) + ". Latest " +
      fmtVal(latest && latest.v, series[0].dec, cfg.unit) +
      ". Exact values are in the table view below the chart.");

    tableTwin(host,
      ["measured (UTC)"].concat(series.map(s => s.label + (cfg.unit ? " (" + cfg.unit.trim() + ")" : ""))),
      series[0].pts.map((p, i) => [fmtDT(p.t)].concat(
        series.map(s => { const q = s.pts[i]; return q && q.v != null ? fmtVal(q.v, s.dec, "") : "—"; }))),
      "table view · " + series[0].pts.length + " readings");

    shareKit(host, {
      kind: "canvas",
      node: cv,
      title: cfg.shareTitle || cfg.ariaTitle,
      legend: series.map(s => ({ label: s.label, color: s.color })),
      axis: { left: fmtD(t0), right: (stale ? "stale · last " : "") + fmtD(t1) },
      clock: "data through " + fmtD(t1),
      dataUrl: cfg.url
    });

    /* hover + keyboard: the reader aims at a date, never at a 2px line */
    function idxAt(clientX) {
      const r = cv.getBoundingClientRect();
      const x = clientX - r.left;
      let best = -1, bd = Infinity;
      for (let i = 0; i < times.length; i++) {
        const d = Math.abs(geom.X(times[i]) - x);
        if (d < bd) { bd = d; best = i; }
      }
      return best;
    }
    function showTip() {
      if (hoverIdx < 0) { tip.hide(); return; }
      const rows = series.map(s => {
        const p = s.pts[hoverIdx];
        return { color: s.color, value: p && p.v != null ? fmtVal(p.v, s.dec, cfg.unit) : "no reading",
                 label: series.length > 1 ? s.label : null };
      });
      const p0 = series[0].pts[hoverIdx];
      tip.show(geom.X(times[hoverIdx]),
               p0 && p0.v != null ? geom.Y(p0.v) : geom.padT + geom.ph / 2,
               fmtDT(times[hoverIdx]), rows);
    }
    cv.addEventListener("pointermove", e => { hoverIdx = idxAt(e.clientX); draw(1); showTip(); });
    // A tap emits pointerdown/up with no movement, so a touch reader would
    // never see a value without discovering drag-scrubbing by accident.
    cv.addEventListener("pointerdown", e => { hoverIdx = idxAt(e.clientX); draw(1); showTip(); });
    cv.addEventListener("pointerleave", () => { hoverIdx = -1; draw(1); tip.hide(); });
    cv.addEventListener("keydown", e => {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const d = e.key === "ArrowLeft" ? -1 : 1;
        hoverIdx = hoverIdx < 0 ? times.length - 1
          : Math.max(0, Math.min(times.length - 1, hoverIdx + d));
        draw(1); showTip();
      } else if (e.key === "Home") { hoverIdx = 0; draw(1); showTip(); }
      else if (e.key === "End") { hoverIdx = times.length - 1; draw(1); showTip(); }
      else if (e.key === "Escape") { hoverIdx = -1; draw(1); tip.hide(); }
    });
    cv.addEventListener("blur", () => { hoverIdx = -1; draw(1); tip.hide(); });

    if ("ResizeObserver" in window) {
      let raf = 0;
      new ResizeObserver(() => {
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => draw(1));
      }).observe(wrap);
    }
    reveal(draw);
  }

  /* ======================================================== SPARKLINE ======
     Decorative recent-trend beside a stat the page already states in words.
     Hidden entirely until the history justifies it. De-emphasis stroke, the
     current period in the accent — never a second way of shouting.
     ======================================================================== */
  function spark(node, cfg) {
    const t = tokens();
    loadHistory(cfg.url).then(rows => {
      const pts = rows.map(r => ({ t: rowTime(r), v: num(pick(r, cfg.key)) }))
                      .filter(p => isFinite(p.t) && p.v != null)
                      .slice(-(cfg.n || 40));
      if (pts.length < (cfg.min || 6)) return;
      node.classList.add("is-on");
      node.setAttribute("aria-hidden", "true");
      const cv = el("canvas");
      node.appendChild(cv);
      function draw() {
        const { ctx, w, h } = sizeCanvas(cv, 30);
        ctx.clearRect(0, 0, w, h);
        let mn = Infinity, mx = -Infinity;
        for (const p of pts) { if (p.v < mn) mn = p.v; if (p.v > mx) mx = p.v; }
        const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
        const X = v => 2 + ((v - t0) / Math.max(1, t1 - t0)) * (w - 12);
        const Y = v => 3 + (1 - (v - mn) / Math.max(1e-9, mx - mn)) * (h - 8);
        ctx.beginPath();
        pts.forEach((p, i) => { if (i) ctx.lineTo(X(p.t), Y(p.v)); else ctx.moveTo(X(p.t), Y(p.v)); });
        ctx.strokeStyle = rgba(t.text4, 0.6);
        ctx.lineWidth = 1.5;
        ctx.lineJoin = "round";
        ctx.stroke();
        const e = pts[pts.length - 1];
        ctx.beginPath(); ctx.arc(X(e.t), Y(e.v), 3, 0, 7);
        ctx.fillStyle = t.live; ctx.fill();
        ctx.lineWidth = 1.5; ctx.strokeStyle = surfaceOf(node); ctx.stroke();
      }
      if ("ResizeObserver" in window) new ResizeObserver(draw).observe(node);
      draw();
    }).catch(() => { /* a decorative element earns no error state */ });
  }
  function autowire() {
    document.querySelectorAll("[data-spark]").forEach(n => {
      spark(n, { url: n.getAttribute("data-spark"), key: n.getAttribute("data-spark-key") });
    });
  }

  /* ===================================================== ALARM RASTER ======
     Rows of signals, columns of committed runs; state carried by words in the
     legend and by glyph + colour in the cell, never colour alone.
     cfg = { url, groups: [{label, rows:[{key,label}]}], maxRuns }
     ======================================================================== */
  const GLYPH = { alarm: "▲", watch: "◆", no_data: "–", absent: "",
                  stale: "×", closed: "■" };
  function raster(host, cfg) {
    loadHistory(cfg.url).then(all => {
      const runs = all.slice(-(cfg.maxRuns || 60));
      if (!runs.length) { failNote(host, cfg.url, "no runs on record yet"); return; }
      const fb = fallbackEl(host);
      if (fb) fb.remove();

      const scroll = scrollBox("pc-raster__scroll", "alarm history grid");
      const grid = el("div", "pc-raster__grid");
      // Single-sourced with .pc-cell in charts.css so a coarse-pointer bump in
      // one place cannot desynchronise the grid track from the button size.
      grid.style.gridTemplateColumns =
        "max-content repeat(" + runs.length + ", var(--pc-cell-size, 17px))";
      const wrap = el("div", "pc-raster pc__wrap");
      const tip = makeTip(wrap);

      const stateOf = (run, key) => {
        const s = (run.states || {})[key];
        return s == null ? "absent" : s;
      };

      for (const group of cfg.groups) {
        const gl = el("div", "pc-raster__lay", group.label);
        gl.style.gridColumn = "1 / -1";
        grid.appendChild(gl);
        for (const row of group.rows) {
          grid.appendChild(el("div", "pc-raster__lab", row.label));
          runs.forEach(run => {
            const st = stateOf(run, row.key);
            const cell = el("button", "pc-cell", GLYPH[st] != null ? GLYPH[st] : "");
            cell.type = "button";
            cell.dataset.state = st;
            const when = fmtDT(Date.parse(run.generated_at));
            cell.setAttribute("aria-label", row.label + ": " +
              st.replace(/_/g, " ") + ", " + when);
            const show = () => {
              const r = cell.getBoundingClientRect(), w = wrap.getBoundingClientRect();
              tip.show(r.left - w.left + 8, r.top - w.top, when,
                [{ value: st.replace(/_/g, " "), label: row.label }]);
            };
            cell.addEventListener("pointerenter", show);
            cell.addEventListener("focus", show);
            cell.addEventListener("pointerleave", tip.hide);
            cell.addEventListener("blur", tip.hide);
            grid.appendChild(cell);
          });
        }
      }
      scroll.appendChild(grid);
      wrap.appendChild(scroll);
      host.appendChild(wrap);

      const key = el("div", "pc-key");
      [["calm", "calm"], ["warming_up", "warming up"], ["watch", "◆ watch"],
       ["alarm", "▲ alarm"], ["no_data", "– no data"], ["absent", "not yet armed"]]
        .forEach(([st, label]) => {
          const item = el("span", null, label);
          const c = el("span", "pc-cell", GLYPH[st] || "");
          c.dataset.state = st;
          item.prepend(c);
          key.appendChild(item);
        });
      host.appendChild(key);

      const t0 = Date.parse(runs[0].generated_at);
      const t1 = Date.parse(runs[runs.length - 1].generated_at);
      host.appendChild(el("p", "pc__foot",
        runs.length + " committed state changes on record, " + fmtD(t0) + " to " + fmtD(t1) +
        ". A row is committed only when some flag changes, so columns are events, not equal " +
        "steps of time — hover any cell for its date. A short strip is a young instrument, " +
        "not a quiet country."));

      shareKit(host, { title: cfg.shareTitle, dataUrl: cfg.url });
    }).catch(() => failNote(host, cfg.url));
  }

  /* ====================================================== DRIFT MATRIX =====
     Models × frozen probes from the latest sealed drift reading. A cell is a
     word-backed glyph; a ring marks a refusal new in this run.
     ======================================================================== */
  function driftMatrix(host, cfg) {
    loadJson(cfg.url).then(d => {
      const models = d.models || [];
      if (!models.length) { failNote(host, cfg.url, "no drift reading"); return; }
      const fb = fallbackEl(host);
      if (fb) fb.remove();

      const probes = Object.keys(models[0].labels || {}).sort();
      const scroll = scrollBox("ps-scroll-x", "drift matrix");
      const table = el("table", "pc-matrix");
      const thead = el("thead"), trh = el("tr");
      trh.appendChild(el("th", null, "frozen probe"));
      for (const m of models) trh.appendChild(el("th", null, (m.model || "").split("/").pop()));
      thead.appendChild(trh);
      table.appendChild(thead);

      const tbody = el("tbody");
      for (const probe of probes) {
        const tr = el("tr");
        tr.appendChild(el("td", null, probe));
        for (const m of models) {
          const td = el("td");
          const state = (m.labels || {})[probe] || "not run";
          const fresh = ((m.drift_vs_prior || {}).new_refusals || []).includes(probe);
          let cls = "pc-chip--answered", glyph = "·";
          if (state === "refused") { cls = "pc-chip--refused"; glyph = "✕"; }
          else if (state !== "answered") { cls = "pc-chip--stale"; glyph = "?"; }
          const chip = el("span", "pc-chip " + cls + (fresh ? " pc-chip--new" : ""), glyph);
          chip.setAttribute("role", "img");
          chip.setAttribute("aria-label", state + (fresh ? ", newly refused this run" : ""));
          td.appendChild(chip);
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
      scroll.appendChild(table);
      host.appendChild(scroll);

      const key = el("div", "pc-key");
      [["pc-chip--answered", "·", "answered"], ["pc-chip--refused", "✕", "refused"],
       ["pc-chip--refused pc-chip--new", "✕", "newly refused this run"],
       ["pc-chip--stale", "?", "did not run"]].forEach(([cls, g, label]) => {
        const item = el("span", null, label);
        const c = el("span", "pc-chip " + cls, g);
        c.style.marginRight = "6px";
        item.prepend(c);
        key.appendChild(item);
      });
      host.appendChild(key);

      const drifted = models.filter(m =>
        ((m.drift_vs_prior || {}).new_refusals || []).length).length;
      host.appendChild(el("p", "pc__foot",
        "Sealed " + (d.generated_at || "").slice(0, 10) + " · " + probes.length +
        " frozen benign probes × " + models.length + " models. " +
        (drifted ? drifted + " model(s) newly refused a probe this run."
                 : "No model newly refused a probe this run — the grid is the evidence either way.")));

      shareKit(host, { title: cfg.shareTitle, dataUrl: cfg.url });
    }).catch(() => failNote(host, cfg.url));
  }

  /* ======================================================= VANTAGE MAP =====
     Where the instrument sees from, inside China. Deliberately borderless:
     a graticule and named cities, because this maps the instrument, not the
     territory. Dot size = observations at that city this round; fill = share
     of determinate answers that came back forged. Counts ride the tooltip and
     the table, so colour never carries the finding alone.
     ======================================================================== */
  const GAZ = {
    /* city: [lon, lat, labelDx, labelDy, anchor] — datacentre metros the
       Globalping CN pool actually offers, plus room for the draw to widen */
    "Beijing":      [116.41, 39.90,  8, -6, "left"],
    "Tianjin":      [117.20, 39.13,  9,  8, "left"],
    "Zhangjiakou":  [114.88, 40.81, -8, -8, "right"],
    "Hohhot":       [111.75, 40.84, -8, -6, "right"],
    "Ulanqab":      [113.13, 40.99,  8, -12, "left"],
    "Dalian":       [121.61, 38.91,  9,  0, "left"],
    "Shenyang":     [123.43, 41.81,  9, -4, "left"],
    "Harbin":       [126.53, 45.80,  9, -4, "left"],
    "Jinan":        [117.12, 36.65,  9,  2, "left"],
    "Qingdao":      [120.38, 36.07,  9,  4, "left"],
    "Zhengzhou":    [113.63, 34.75, -9,  2, "right"],
    "Xi'an":        [108.94, 34.34, -9,  0, "right"],
    "Lanzhou":      [103.83, 36.06, -9, -4, "right"],
    "Nanjing":      [118.80, 32.06, -10, -4, "right"],
    "Shanghai":     [121.47, 31.23, 10,  2, "left"],
    "Suzhou":       [120.58, 31.30, -10, 10, "right"],
    "Hangzhou":     [120.15, 30.27,  8, 13, "left"],
    "Hefei":        [117.23, 31.82, -9, -4, "right"],
    "Wuhan":        [114.30, 30.59,  9,  4, "left"],
    "Chengdu":      [104.07, 30.57, -9,  4, "right"],
    "Chongqing":    [106.55, 29.56,  9,  8, "left"],
    "Changsha":     [112.94, 28.23, -9,  6, "right"],
    "Nanchang":     [115.86, 28.68,  9,  2, "left"],
    "Fuzhou":       [119.30, 26.07,  9,  2, "left"],
    "Xiamen":       [118.09, 24.48,  9,  4, "left"],
    "Guiyang":      [106.63, 26.65, -9,  4, "right"],
    "Kunming":      [102.83, 24.88, -9,  4, "right"],
    "Nanning":      [108.32, 22.82, -9,  6, "right"],
    "Guangzhou":    [113.26, 23.13, -10, -6, "right"],
    "Dongguan":     [113.75, 23.02, 10,  10, "left"],
    "Foshan":       [113.12, 23.02, -10,  8, "right"],
    "Shenzhen":     [114.06, 22.55, 10,  2, "left"],
    "Hong Kong":    [114.17, 22.32, 10,  10, "left"],
    "Haikou":       [110.33, 20.03, -9,  4, "right"],
    "Urumqi":       [ 87.62, 43.83, -9, -4, "right"],
    "Lhasa":        [ 91.11, 29.65, -9,  4, "right"],
    "Xining":       [101.78, 36.62, -9, -8, "right"],
    "Yinchuan":     [106.23, 38.49, -9, -4, "right"],
    "Taiyuan":      [112.55, 37.87,  9, -4, "left"],
    "Shijiazhuang": [114.51, 38.04, -10,  6, "right"],
    "Changchun":    [125.32, 43.90,  9, -4, "left"],
    "Wuxi":         [120.31, 31.57, 10, -6, "left"],
    "Ningbo":       [121.55, 29.87, 10,  4, "left"],
    "Wenzhou":      [120.70, 28.00, 10,  2, "left"],
    "Baoding":      [115.46, 38.87, -10,  2, "right"]
  };

  function aggregateVantages(reading) {
    const cities = {}, networks = {}, unknown = new Set();
    for (const dom of reading.domains || []) {
      for (const v of dom.vantages || []) {
        const city = v.city || "unknown";
        if (!GAZ[city]) unknown.add(city);
        const c = cities[city] || (cities[city] = {
          forged: 0, clean: 0, undetermined: 0, silent: 0, other: 0, nets: new Set()
        });
        const st = ["forged", "clean", "undetermined", "silent"].includes(v.state) ? v.state : "other";
        c[st] += 1;
        if (v.network) { c.nets.add(v.network); }
        if (v.network) {
          const n = networks[v.network] || (networks[v.network] = {
            forged: 0, clean: 0, undetermined: 0, silent: 0, other: 0
          });
          n[st] += 1;
        }
      }
    }
    return { cities, networks, unknown: [...unknown].filter(u => u !== "unknown") };
  }

  function vantageMap(host, cfg) {
    const t = tokens();
    return loadJson(cfg.url).then(d => {
      const agg = aggregateVantages(d);
      const names = Object.keys(agg.cities).filter(c => GAZ[c]);
      if (!names.length) { failNote(host, cfg.url, "no vantages in the latest round"); return agg; }
      const fb = fallbackEl(host);
      if (fb) fb.remove();

      const wrap = el("div", "pc-map pc__wrap");
      const cv = el("canvas");
      cv.setAttribute("tabindex", "0");
      cv.setAttribute("role", "img");
      wrap.appendChild(cv);
      host.appendChild(wrap);
      const tip = makeTip(wrap);

      /* ramp legend + the honest note about what is not drawn */
      const ramp = el("div", "pc-map__ramp");
      ramp.appendChild(document.createTextNode("0% forged"));
      ramp.appendChild(el("i"));
      ramp.appendChild(document.createTextNode("100% · share of determinate answers · dot size = observations"));
      host.appendChild(ramp);

      const surface = surfaceOf(host);
      const total = c => c.forged + c.clean + c.undetermined + c.silent + c.other;
      const det = c => c.forged + c.clean;
      let focus = -1;
      let placed = [];

      function draw() {
        const H = Math.max(300, Math.min(420, wrap.clientWidth * 0.62));
        const { ctx, w, h } = sizeCanvas(cv, H);
        ctx.clearRect(0, 0, w, h);

        /* fit mainland bbox, latitude-true */
        const LON0 = 84, LON1 = 130, LAT0 = 17.5, LAT1 = 46.5;
        const cosMid = Math.cos((LAT0 + LAT1) / 2 * Math.PI / 180);
        const pad = 26;
        const sx = (w - pad * 2) / ((LON1 - LON0) * cosMid);
        const sy = (h - pad * 2) / (LAT1 - LAT0);
        const s = Math.min(sx, sy);
        const ox = (w - (LON1 - LON0) * cosMid * s) / 2;
        const oy = (h - (LAT1 - LAT0) * s) / 2;
        const X = lon => ox + (lon - LON0) * cosMid * s;
        const Y = lat => oy + (LAT1 - lat) * s;

        /* graticule: the only geography drawn, on purpose */
        ctx.strokeStyle = t.grid;
        ctx.lineWidth = 1;
        ctx.fillStyle = t.text4;
        ctx.font = "9px " + t.mono;
        for (let lon = 90; lon <= 125; lon += 10) {
          const x = Math.round(X(lon)) + 0.5;
          ctx.beginPath(); ctx.moveTo(x, oy); ctx.lineTo(x, Y(LAT0)); ctx.stroke();
          ctx.fillText(lon + "°E", x - 12, Y(LAT0) + 12);
        }
        for (let lat = 20; lat <= 45; lat += 10) {
          const y = Math.round(Y(lat)) + 0.5;
          ctx.beginPath(); ctx.moveTo(X(LON0), y); ctx.lineTo(X(LON1), y); ctx.stroke();
          ctx.fillText(lat + "°N", X(LON0) - 24, y + 3);
        }

        placed = names.map(name => {
          const g = GAZ[name], c = agg.cities[name];
          const n = total(c), dt = det(c);
          return {
            name, c,
            x: X(g[0]), y: Y(g[1]),
            r: Math.min(15, 4 + 2.6 * Math.sqrt(n)),
            share: dt > 0 ? c.forged / dt : null,
            dx: g[2], dy: g[3], anchor: g[4]
          };
        });

        /* dots: sequential fill for magnitude, stale token where nothing is
           determinate; a 2px surface ring keeps neighbours apart */
        for (const p of placed) {
          ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 7);
          ctx.fillStyle = p.share == null ? t.stale : rampColor(p.share);
          ctx.fill();
          ctx.lineWidth = 2;
          ctx.strokeStyle = surface;
          ctx.stroke();
        }
        /* focus ring for keyboard users */
        if (focus >= 0 && placed[focus]) {
          const p = placed[focus];
          ctx.beginPath(); ctx.arc(p.x, p.y, p.r + 4, 0, 7);
          ctx.strokeStyle = t.live; ctx.lineWidth = 2; ctx.stroke();
        }
        /* labels in text tokens, never the data colour.
           On a phone the canvas is ~330px wide and the delta clusters put a
           dozen 10px labels on top of each other — an unreadable smear that
           also hides the dots. Below 480px only the largest cities are
           labelled; every city stays tappable and the table twin carries the
           full list, so nothing is lost, only decluttered. */
        const wide = w >= 480;
        const named = wide ? null : new Set(
          placed.slice().sort((a, b) => b.r - a.r).slice(0, 8).map(p => p.name));
        ctx.font = "10px " + t.mono;
        for (const p of placed) {
          if (named && !named.has(p.name)) continue;
          ctx.fillStyle = t.text3;
          ctx.textAlign = p.anchor === "right" ? "right" : "left";
          ctx.fillText(p.name, p.x + p.dx + (p.anchor === "right" ? -p.r + 4 : p.r - 4), p.y + p.dy + 3);
        }
        ctx.textAlign = "left";
      }

      function describe(p) {
        const c = p.c;
        const rows = [
          { color: t.crit, value: String(c.forged), label: "forged" },
          { color: t.ok, value: String(c.clean), label: "clean" },
          { color: t.stale, value: String(c.undetermined), label: "undetermined" }
        ];
        if (c.silent) rows.push({ color: t.stale, value: String(c.silent), label: "silent" });
        rows.push({ value: p.share == null ? "n/a" : Math.round(p.share * 100) + "%",
                    label: "forged share of " + det(c) + " determinate" });
        return rows;
      }
      function showTip(p) {
        tip.show(p.x, p.y - p.r, p.name + " · " + [...p.c.nets].length + " network" +
                 ([...p.c.nets].length === 1 ? "" : "s"), describe(p));
      }
      cv.addEventListener("pointermove", e => {
        const r = cv.getBoundingClientRect();
        const x = e.clientX - r.left, y = e.clientY - r.top;
        let best = null, bd = 30;
        for (const p of placed) {
          const d = Math.hypot(p.x - x, p.y - y) - p.r;
          if (d < bd) { bd = d; best = p; }
        }
        if (best) showTip(best); else tip.hide();
      });
      /* A tap emits no pointermove, so touch readers need the same hit test. */
      cv.addEventListener("pointerdown", e => {
        const r = cv.getBoundingClientRect();
        const x = e.clientX - r.left, y = e.clientY - r.top;
        let best = null, bd = 30;
        for (const p of placed) {
          const d = Math.hypot(p.x - x, p.y - y) - p.r;
          if (d < bd) { bd = d; best = p; }
        }
        if (best) showTip(best); else tip.hide();
      });
      cv.addEventListener("pointerleave", tip.hide);
      cv.addEventListener("keydown", e => {
        if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
          e.preventDefault();
          const order = placed.map((p, i) => i).sort((a, b) => placed[a].x - placed[b].x);
          const pos = order.indexOf(focus);
          const next = e.key === "ArrowRight"
            ? order[Math.min(order.length - 1, pos + 1)]
            : order[Math.max(0, pos < 0 ? 0 : pos - 1)];
          focus = next == null ? order[0] : next;
          draw();
          showTip(placed[focus]);
        } else if (e.key === "Escape") { focus = -1; draw(); tip.hide(); }
      });
      cv.addEventListener("blur", () => { focus = -1; draw(); tip.hide(); });

      cv.setAttribute("aria-label", "Vantage map: " + names.length +
        " mainland cities with live probes this round. Dot size is observations, " +
        "fill is the share of determinate answers that came back forged. Exact " +
        "counts per city are in the table view below.");

      tableTwin(host,
        ["city", "observations", "forged", "clean", "undetermined", "silent", "forged share"],
        names.slice().sort((a, b) => det(agg.cities[b]) - det(agg.cities[a])).map(n => {
          const c = agg.cities[n], dt = det(c);
          return [n, String(total(c)), String(c.forged), String(c.clean),
                  String(c.undetermined), String(c.silent),
                  dt ? Math.round(100 * c.forged / dt) + "% of " + dt : "no determinate answers"];
        }),
        "table view · per-city counts");

      if (agg.unknown.length) {
        host.appendChild(el("p", "pc__foot",
          "Seen this round but not drawn (no fixed coordinates yet): " +
          agg.unknown.join(", ") + "."));
      }

      shareKit(host, {
        kind: "canvas",
        node: cv,
        title: cfg.shareTitle,
        note: "dot size = observations · fill = share of determinate answers forged",
        clock: d.generated_at ? "round sealed " + String(d.generated_at).slice(0, 10) : null,
        dataUrl: cfg.url
      });

      if ("ResizeObserver" in window) {
        let raf = 0;
        new ResizeObserver(() => {
          cancelAnimationFrame(raf);
          raf = requestAnimationFrame(draw);
        }).observe(wrap);
      }
      draw();
      return agg;
    }).catch(err => { failNote(host, cfg.url); throw err; });
  }

  /* The second angle on the same data: who operates the networks the probes
     sit on. Counts as a gapped strip per operator, words on every segment. */
  function operatorStrip(host, networks) {
    const order = Object.keys(networks).sort((a, b) =>
      (networks[b].forged + networks[b].clean) - (networks[a].forged + networks[a].clean));
    if (!order.length) return;
    const fb = fallbackEl(host);
    if (fb) fb.remove();
    const grid = el("div", "pc-ops");
    for (const name of order) {
      const n = networks[name];
      const row = el("div", "pc-ops__row");
      const head = el("p", "pc-ops__name", name);
      const tot = n.forged + n.clean + n.undetermined + n.silent + n.other;
      head.appendChild(el("span", null,
        n.forged + " forged · " + n.clean + " clean · " +
        (n.undetermined + n.silent + n.other) + " indeterminate · " + tot + " total"));
      row.appendChild(head);
      const bar = el("div", "pc-ops__bar");
      bar.setAttribute("role", "img");
      bar.setAttribute("aria-label", name + ": " + n.forged + " forged, " + n.clean +
        " clean, " + (n.undetermined + n.silent + n.other) + " indeterminate observations");
      [["forged", n.forged], ["clean", n.clean],
       ["undetermined", n.undetermined + n.other], ["silent", n.silent]].forEach(([k, v]) => {
        if (!v) return;
        const seg = el("div", "pc-ops__seg pc-ops__seg--" + k);
        seg.style.flexGrow = String(v);
        bar.appendChild(seg);
      });
      row.appendChild(bar);
      grid.appendChild(row);
    }
    host.appendChild(grid);
  }

  /* ====================================================== CALIBRATION ======
     The forecast ledger's own report card: per-signal empirical coverage
     against the nominal target, with the baseline verdict in words.
     ======================================================================== */
  function calibration(host, cfg) {
    loadHistory(cfg.url).then(rows => {
      const d = rows[rows.length - 1];
      const per = (d && d.per_signal) || {};
      const keys = Object.keys(per);
      if (!keys.length) { failNote(host, cfg.url, "no scored forecasts yet"); return; }
      const fb = fallbackEl(host);
      if (fb) fb.remove();

      const labels = cfg.labels || {};
      const nominal = num(d.nominal_coverage) || 0.8;
      keys.sort((a, b) => (per[b].n || 0) - (per[a].n || 0));

      const W = 600, ROW = 26, top = 26, bottom = 24, labW = 150, chipW = 158;
      const H = top + keys.length * ROW + bottom;
      const x0 = labW, x1 = W - chipW;
      const NS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(NS, "svg");
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", "Forecast calibration: empirical interval coverage per signal " +
        "against the " + Math.round(nominal * 100) + " percent target. Exact values in the table view.");
      const mk = (name, attrs, text) => {
        const n = document.createElementNS(NS, name);
        for (const k in attrs) n.setAttribute(k, attrs[k]);
        if (text != null) n.textContent = text;
        return n;
      };
      const CX = v => x0 + (Math.max(0.5, Math.min(1, v)) - 0.5) / 0.5 * (x1 - x0);

      for (const v of [0.5, 0.6, 0.7, 0.9, 1.0]) {
        svg.appendChild(mk("line", { x1: CX(v), y1: top - 8, x2: CX(v), y2: H - bottom, "class": "pc-cal__grid" }));
        svg.appendChild(mk("text", { x: CX(v) - 10, y: H - 8, "class": "pc-cal__tick" }, Math.round(v * 100) + "%"));
      }
      svg.appendChild(mk("line", { x1: CX(nominal), y1: top - 12, x2: CX(nominal), y2: H - bottom, "class": "pc-cal__nom" }));
      svg.appendChild(mk("text", { x: CX(nominal) - 26, y: 12, "class": "pc-cal__tick" }, "target " + Math.round(nominal * 100) + "%"));

      keys.forEach((k, i) => {
        const y = top + i * ROW + ROW / 2;
        const p = per[k];
        svg.appendChild(mk("text", { x: 0, y: y + 3 }, labels[k] || k));
        const cov = num(p.coverage);
        if (cov != null) {
          svg.appendChild(mk("circle", { cx: CX(cov), cy: y, r: 5, "class": "pc-cal__dot" }));
        }
        const beats = p.beats_baseline === true;
        const verdictText = beats ? "✓ beats baseline" : "✕ under baseline";
        const vt = mk("text", { x: x1 + 12, y: y + 3 }, verdictText + " · n=" + (p.n != null ? p.n : "?"));
        vt.style.fill = beats ? tokens().ok : tokens().warn;
        svg.appendChild(vt);
      });

      // The plot's coordinate system is fixed at 600 units wide with a 150-unit
      // label gutter. Letting it shrink to a phone's width scales the 9px tick
      // type to ~5px — legible to nobody. It scrolls at its readable size
      // instead, inside the house pattern, with the table twin below it.
      const box = el("div", "pc-cal");
      svg.style.minWidth = "560px";
      const calScroll = scrollBox("ps-scroll-x", "forecast calibration plot");
      calScroll.appendChild(svg);
      box.appendChild(calScroll);
      host.appendChild(box);

      tableTwin(host,
        ["signal", "coverage", "target", "weighted interval score", "beats climatology baseline", "forecasts scored"],
        keys.map(k => {
          const p = per[k];
          return [labels[k] || k,
                  p.coverage != null ? Math.round(p.coverage * 1000) / 10 + "%" : "—",
                  Math.round(nominal * 100) + "%",
                  p.wis != null ? String(p.wis) : "—",
                  p.beats_baseline === true ? "yes" : "no",
                  String(p.n != null ? p.n : "—")];
        }),
        "table view · per-signal scores");

      host.appendChild(el("p", "pc__foot",
        "Pooled coverage " + (d.pooled_empirical_coverage != null
          ? Math.round(d.pooled_empirical_coverage * 1000) / 10 + "%" : "—") +
        " across " + (d.n_forecasts != null ? d.n_forecasts : "—") +
        " scored forecasts, against a " + Math.round(nominal * 100) + "% target. " +
        "A signal beating the climatology baseline earned its interval; one under it is said so here."));

      shareKit(host, {
        kind: "svg",
        node: svg,
        title: cfg.shareTitle,
        clock: isFinite(rowTime(d)) ? "scored through " + fmtD(rowTime(d)) : null,
        dataUrl: cfg.url
      });
    }).catch(() => failNote(host, cfg.url));
  }

  /* ========================================================= SHARE KIT =====
     Every chart can leave the page as a self-describing PNG: its title, its
     legend, the exact pixels on screen, and a footer that cites the page AND
     the raw data file — a screenshot that carries its own provenance. DOM
     grids (raster, drift matrix) get a copy-link only; a redrawn imitation
     of them would not be the published grid, so it is not offered.
     ======================================================================== */
  function guessTitle(host, given) {
    if (given) return given;
    let n = host.previousElementSibling;
    while (n) {
      if (/^H[1-4]$/.test(n.tagName)) return n.textContent.trim();
      n = n.previousElementSibling;
    }
    const sec = host.closest("section, article, main");
    const h = sec && sec.querySelector("h1, h2, h3");
    return h ? h.textContent.trim() : document.title;
  }
  function pageLink(host) {
    const base = location.origin + location.pathname;
    return host.id ? base + "#" + host.id : base + location.hash;
  }
  function absUrl(u) {
    try { return new URL(u, location.href).href; } catch (e) { return u; }
  }
  function slugOf(s) {
    return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "")
            .slice(0, 48) || "chart";
  }

  /* The on-page surfaces are translucent washes over the void, so the first
     painted ancestor is NOT a usable export ground — a PNG filled with
     rgba(255,255,255,.03) is a transparent image. Flatten the whole ancestor
     stack (host up through body and html) over an opaque dark base instead,
     so the export ground is exactly what the reader's eye sees. */
  function exportSurface(host) {
    const parse = c => {
      const m = /rgba?\(([^)]+)\)/.exec(c || "");
      if (!m) return null;
      const p = m[1].split(",").map(parseFloat);
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    };
    const layers = [];
    let n = host;
    while (n) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0) layers.push(c);
      n = n.parentElement;
    }
    let r = 14, g = 14, b = 14; /* #0e0e0e when nothing paints at all */
    for (let i = layers.length - 1; i >= 0; i--) {
      const c = layers[i];
      r = c.r * c.a + r * (1 - c.a);
      g = c.g * c.a + g * (1 - c.a);
      b = c.b * c.a + b * (1 - c.a);
    }
    return "rgb(" + Math.round(r) + "," + Math.round(g) + "," + Math.round(b) + ")";
  }
  function stampName(title) {
    const d = new Date();
    return "palimpsest-" + slugOf(title) + "-" + d.getUTCFullYear() +
           pad2(d.getUTCMonth() + 1) + pad2(d.getUTCDate()) + ".png";
  }

  /* An SVG chart is styled by charts.css; a serialized copy loses that, so
     the computed styles are inlined onto a clone before rasterising. */
  function svgImage(svg) {
    return new Promise((res, rej) => {
      const clone = svg.cloneNode(true);
      const src = [svg].concat([].slice.call(svg.querySelectorAll("*")));
      const dst = [clone].concat([].slice.call(clone.querySelectorAll("*")));
      const PROPS = ["fill", "stroke", "stroke-width", "stroke-dasharray",
                     "font-family", "font-size", "font-weight", "opacity",
                     "text-anchor", "letter-spacing"];
      for (let i = 0; i < src.length; i++) {
        const cs = getComputedStyle(src[i]);
        let style = "";
        for (const p of PROPS) {
          const v = cs.getPropertyValue(p);
          if (v) style += p + ":" + v + ";";
        }
        if (style) dst[i].setAttribute("style", style);
      }
      const vb = svg.viewBox.baseVal;
      clone.setAttribute("width", vb.width);
      clone.setAttribute("height", vb.height);
      clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      const img = new Image();
      img.onload = () => res({ img, w: vb.width, h: vb.height });
      img.onerror = () => rej(new Error("svg rasterise failed"));
      img.src = "data:image/svg+xml;charset=utf-8," +
                encodeURIComponent(new XMLSerializer().serializeToString(clone));
    });
  }

  /* Compose the card. cfgS: { title, node, kind, legend, axis, note, clock,
     dataUrl, host }. Returns a Promise<canvas>. */
  function composeCard(cfgS) {
    const t = tokens();
    const EX = 2; /* supersample — crisp after every platform recompress */
    const drawInto = (chartW, chartH, paint) => {
      const pad = 20;
      const W = Math.max(560, chartW + pad * 2);
      const innerW = W - pad * 2;
      const drawH = Math.round(chartH * (innerW / chartW));
      const titleY = 28;
      const legendY = cfgS.legend && cfgS.legend.length ? titleY + 18 : titleY;
      const chartY = legendY + 12;
      const axisY = cfgS.axis ? chartY + drawH + 14 : chartY + drawH;
      const noteY = cfgS.note ? axisY + 16 : axisY;
      const ruleY = noteY + 12;
      const f1Y = ruleY + 16;
      const f2Y = f1Y + 14;
      const H = f2Y + 12;

      const cv = document.createElement("canvas");
      cv.width = W * EX;
      cv.height = H * EX;
      const ctx = cv.getContext("2d");
      ctx.scale(EX, EX);

      ctx.fillStyle = exportSurface(cfgS.host);
      ctx.fillRect(0, 0, W, H);
      ctx.strokeStyle = t.grid;
      ctx.lineWidth = 1;
      ctx.strokeRect(0.5, 0.5, W - 1, H - 1);

      ctx.fillStyle = t.text1;
      ctx.font = "600 13px " + t.mono;
      ctx.fillText(cfgS.title, pad, titleY);
      ctx.font = "9px " + t.mono;
      ctx.fillStyle = t.text3;
      const mark = "PALIMPSEST · palimpsest.info";
      ctx.fillText(mark, W - pad - ctx.measureText(mark).width, titleY);

      if (cfgS.legend && cfgS.legend.length) {
        ctx.font = "9.5px " + t.mono;
        let lx = pad;
        for (const s of cfgS.legend) {
          ctx.fillStyle = s.color;
          ctx.fillRect(lx, legendY - 6, 9, 3);
          lx += 14;
          ctx.fillStyle = t.text2;
          ctx.fillText(s.label, lx, legendY);
          lx += ctx.measureText(s.label).width + 16;
        }
      }

      paint(ctx, pad, chartY, innerW, drawH);

      ctx.font = "9px " + t.mono;
      if (cfgS.axis) {
        ctx.fillStyle = t.text4;
        ctx.fillText(cfgS.axis.left, pad, axisY);
        ctx.fillText(cfgS.axis.right,
          W - pad - ctx.measureText(cfgS.axis.right).width, axisY);
      }
      if (cfgS.note) {
        ctx.fillStyle = t.text4;
        ctx.fillText(cfgS.note, pad, noteY);
      }

      ctx.strokeStyle = t.grid;
      ctx.beginPath();
      ctx.moveTo(pad, ruleY + 0.5);
      ctx.lineTo(W - pad, ruleY + 0.5);
      ctx.stroke();

      ctx.fillStyle = t.text2;
      ctx.fillText(pageLink(cfgS.host).replace(/^https?:\/\//, ""), pad, f1Y);
      const ex = "exported " + fmtDT(Date.now());
      ctx.fillStyle = t.text4;
      ctx.fillText(ex, W - pad - ctx.measureText(ex).width, f1Y);
      if (cfgS.dataUrl) {
        ctx.fillText("data: " + absUrl(cfgS.dataUrl).replace(/^https?:\/\//, ""), pad, f2Y);
      }
      if (cfgS.clock) {
        ctx.fillText(cfgS.clock, W - pad - ctx.measureText(cfgS.clock).width, f2Y);
      }
      return cv;
    };

    if (cfgS.kind === "svg") {
      return svgImage(cfgS.node).then(({ img, w, h }) =>
        drawInto(w, h, (ctx, x, y, dw, dh) => ctx.drawImage(img, x, y, dw, dh)));
    }
    const cv = cfgS.node;
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    return Promise.resolve(drawInto(
      Math.round(cv.width / dpr), Math.round(cv.height / dpr),
      (ctx, x, y, dw, dh) => ctx.drawImage(cv, x, y, dw, dh)));
  }

  function cardBlob(cfgS) {
    return composeCard(cfgS).then(cv => new Promise((res, rej) =>
      cv.toBlob(b => b ? res(b) : rej(new Error("toBlob failed")), "image/png")));
  }
  function downloadPng(cfgS) {
    return cardBlob(cfgS).then(blob => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = stampName(cfgS.title);
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    });
  }

  function shareKit(host, cfgS) {
    cfgS.host = host;
    cfgS.title = guessTitle(host, cfgS.title);

    const row = el("div", "pc-share");
    row.setAttribute("role", "group");
    row.setAttribute("aria-label", "share this chart");
    const note = el("span", "pc-share__note");
    note.setAttribute("role", "status");
    let noteTimer = 0;
    const say = msg => {
      note.textContent = msg;
      clearTimeout(noteTimer);
      noteTimer = setTimeout(() => { note.textContent = ""; }, 2200);
    };
    const btn = (label, fn, title) => {
      if (row.childNodes.length) row.appendChild(el("span", "pc-share__dot", "·"));
      const b = el("button", null, label);
      b.type = "button";
      if (title) b.title = title;
      b.addEventListener("click", fn);
      row.appendChild(b);
    };

    if (cfgS.kind) {
      btn("download png", () => {
        downloadPng(cfgS).then(() => say("png saved"), () => say("export failed"));
      }, "download this chart as a png card");
      btn("copy image", () => {
        /* Safari accepts a clipboard write only when the ClipboardItem is
           built synchronously in the gesture, with the blob as a promise. */
        let wrote = false;
        try {
          if (navigator.clipboard && typeof ClipboardItem !== "undefined") {
            navigator.clipboard.write([new ClipboardItem({ "image/png": cardBlob(cfgS) })])
              .then(() => say("image copied"),
                    () => downloadPng(cfgS).then(() => say("copy blocked · png saved"),
                                                 () => say("export failed")));
            wrote = true;
          }
        } catch (e) { /* fall through to the download */ }
        if (!wrote) {
          downloadPng(cfgS).then(() => say("copy blocked · png saved"),
                                 () => say("export failed"));
        }
      }, "copy the chart image to the clipboard");
      let shareable = false;
      try {
        shareable = !!(navigator.canShare && navigator.canShare({
          files: [new File([new Uint8Array(1)], "x.png", { type: "image/png" })]
        }));
      } catch (e) { shareable = false; }
      if (shareable) {
        btn("share", () => {
          cardBlob(cfgS).then(blob => navigator.share({
            files: [new File([blob], stampName(cfgS.title), { type: "image/png" })],
            title: cfgS.title,
            text: pageLink(host)
          })).catch(e => { if (!e || e.name !== "AbortError") say("share failed"); });
        }, "share the chart image");
      }
    }
    btn("copy link", () => {
      navigator.clipboard.writeText(pageLink(host))
        .then(() => say("link copied"), () => say("copy blocked"));
    }, "copy a link to this chart");
    row.appendChild(note);
    host.appendChild(row);
  }

  /* ------------------------------------------------------------- boot ----- */
  function init() { autowire(); }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.PalimpsestCharts = {
    loadHistory, loadJson, trend, spark, autowire, raster,
    driftMatrix, vantageMap, operatorStrip, calibration, tokens, shareKit
  };
})();
