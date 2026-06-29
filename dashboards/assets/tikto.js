/* ============================================================================
   TIKTÓ — signature widgets (vanilla JS, zero dependencies)
   ----------------------------------------------------------------------------
   TiktoNetwork  — the living map: navigable SPACE. Nodes/edges, live motion,
                   risk PROPAGATION (watch a shock spread).
   TiktoTick     — the Tick: navigable TIME. Past actuals (solid) → NOW (glow)
                   → future forecast as a CONFIDENCE CONE you scrub into.
   tiktoRoll     — number roll: a value only animates when it actually changed
                   (motion = signal, never decoration).
   ========================================================================== */
(function (global) {
  'use strict';

  var REDUCED = global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#06d6e0';
  }
  function hexA(hex, a) {
    hex = (hex || '#06d6e0').replace('#', '');
    if (hex.length === 3) hex = hex.split('').map(function (c) { return c + c; }).join('');
    var n = parseInt(hex, 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }
  function fitCanvas(canvas) {
    var dpr = Math.min(global.devicePixelRatio || 1, 2);
    var r = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round(r.width * dpr));
    canvas.height = Math.max(1, Math.round(r.height * dpr));
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: r.width, h: r.height };
  }

  /* ======================= THE LIVING MAP ================================= */
  function TiktoNetwork(canvas, opts) {
    opts = opts || {};
    this.canvas = canvas;
    this.nodes = opts.nodes || [];
    this.edges = opts.edges || [];
    this.t = 0;
    this.wave = null;            // active propagation shock
    this._raf = null;
    this._resize = this.resize.bind(this);
    global.addEventListener('resize', this._resize);
    this.resize();
    this.start();
  }
  TiktoNetwork.prototype.resize = function () {
    var f = fitCanvas(this.canvas); this.ctx = f.ctx; this.w = f.w; this.h = f.h;
  };
  TiktoNetwork.prototype.start = function () {
    var self = this;
    function loop() { self.t += 0.016; self.draw(); self._raf = requestAnimationFrame(loop); }
    if (REDUCED) { this.draw(); } else { loop(); }
  };
  TiktoNetwork.prototype.stop = function () {
    if (this._raf) cancelAnimationFrame(this._raf);
    global.removeEventListener('resize', this._resize);
  };
  /* propagate a shock from a node index — travels outward along edges over time */
  TiktoNetwork.prototype.propagate = function (fromIndex) {
    var reach = {}; reach[fromIndex] = 0;
    // BFS distance (in hops) so the wave lights nodes in order
    var frontier = [fromIndex], depth = 0, guard = 0;
    while (frontier.length && guard++ < 50) {
      var next = [];
      for (var i = 0; i < frontier.length; i++) {
        var n = frontier[i];
        for (var e = 0; e < this.edges.length; e++) {
          var ed = this.edges[e], other = ed[0] === n ? ed[1] : (ed[1] === n ? ed[0] : -1);
          if (other >= 0 && reach[other] === undefined) { reach[other] = depth + 1; next.push(other); }
        }
      }
      frontier = next; depth++;
    }
    this.wave = { from: fromIndex, reach: reach, t0: this.t, speed: 1.1 };
  };
  TiktoNetwork.prototype._px = function (node) {
    return { x: 40 + node.x * (this.w - 80), y: 30 + node.y * (this.h - 70) };
  };
  TiktoNetwork.prototype.draw = function () {
    var ctx = this.ctx, w = this.w, h = this.h, T = this.t;
    ctx.clearRect(0, 0, w, h);
    var live = css('--tk-live'), crit = css('--tk-critical'), warn = css('--tk-warning');

    // wave progress (which hops are currently lit)
    var waveFront = this.wave ? (T - this.wave.t0) * this.wave.speed : -1;

    // edges
    for (var e = 0; e < this.edges.length; e++) {
      var a = this._px(this.nodes[this.edges[e][0]]), b = this._px(this.nodes[this.edges[e][1]]);
      var weight = this.edges[e][2] || 0.5;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
      ctx.strokeStyle = hexA(live, 0.05 + weight * 0.10); ctx.lineWidth = 0.6 + weight * 1.1; ctx.stroke();
      // a small packet travelling the edge — "money moving", live
      if (!REDUCED) {
        var p = (T * (0.18 + weight * 0.12) + e * 0.3) % 1;
        ctx.beginPath();
        ctx.arc(a.x + (b.x - a.x) * p, a.y + (b.y - a.y) * p, 1.4, 0, 6.2832);
        ctx.fillStyle = hexA(live, 0.5); ctx.fill();
      }
    }

    // nodes
    for (var i = 0; i < this.nodes.length; i++) {
      var nd = this.nodes[i], pt = this._px(nd);
      var breathe = REDUCED ? 0 : Math.sin(T * 1.4 + i) * 0.5;
      var lit = this.wave && this.wave.reach[i] !== undefined && this.wave.reach[i] <= waveFront;
      var col = nd.status === 'critical' ? crit : nd.status === 'warning' ? warn : live;
      if (lit) col = crit;
      var r = (nd.r || 5) + breathe + (lit ? 2 : 0);

      // glow
      var g = ctx.createRadialGradient(pt.x, pt.y, 0, pt.x, pt.y, r * 4);
      g.addColorStop(0, hexA(col, lit ? 0.5 : 0.30)); g.addColorStop(1, hexA(col, 0));
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(pt.x, pt.y, r * 4, 0, 6.2832); ctx.fill();
      // core
      ctx.beginPath(); ctx.arc(pt.x, pt.y, r, 0, 6.2832);
      ctx.fillStyle = lit ? col : hexA(col, 0.92); ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = hexA('#000000', 0.6); ctx.stroke();

      // label
      if (nd.label) {
        ctx.font = '10px "JetBrains Mono", monospace';
        ctx.fillStyle = hexA(css('--tk-text-2'), 0.9); ctx.textAlign = 'center';
        ctx.fillText(nd.label, pt.x, pt.y - r - 7);
      }
    }

    // expanding shock ring from source
    if (this.wave) {
      var src = this._px(this.nodes[this.wave.from]);
      var rr = (T - this.wave.t0) * 130;
      if (rr < Math.max(w, h)) {
        ctx.beginPath(); ctx.arc(src.x, src.y, rr, 0, 6.2832);
        ctx.strokeStyle = hexA(crit, Math.max(0, 0.5 - rr / Math.max(w, h))); ctx.lineWidth = 2; ctx.stroke();
      } else { this.wave = null; } // shock dissipated
    }
  };

  /* ======================= THE TICK ======================================= */
  /* x maps time from -past .. +horizon. Past = solid actual series.
     Future = forecast mean + a confidence CONE widening with the horizon.   */
  function TiktoTick(canvas, opts) {
    opts = opts || {};
    this.canvas = canvas;
    this.onScrub = opts.onScrub || function () {};
    this.pastPts = opts.past || this._seed();      // [{v}] oldest..now
    var lastV = this.pastPts[this.pastPts.length - 1].v;
    this.base = opts.base != null ? opts.base : lastV;
    // continuity: shift actuals so the line ends exactly where the forecast cone begins
    if (opts.base != null) {
      var d = opts.base - lastV;
      this.pastPts = this.pastPts.map(function (p) { return { v: p.v + d }; });
    }
    this.drift = opts.drift != null ? opts.drift : -0.06; // forecast slope over horizon
    this.playT = 0;                                 // -1 (oldest) .. 0 (now) .. +1 (max horizon)
    this._resize = this.resize.bind(this);
    global.addEventListener('resize', this._resize);
    this._bindDrag();
    this.resize();
    this.draw();
    this.emit();
  }
  TiktoTick.prototype._seed = function () {
    var pts = [], v = 1.55;
    for (var i = 0; i < 40; i++) { v += (Math.sin(i * 0.5) * 0.02) + (i % 7 === 0 ? -0.03 : 0.004); pts.push({ v: v }); }
    return pts;
  };
  TiktoTick.prototype.resize = function () { var f = fitCanvas(this.canvas); this.ctx = f.ctx; this.w = f.w; this.h = f.h; this.draw(); };
  /* value & confidence band at the current playhead */
  TiktoTick.prototype.sample = function () {
    if (this.playT <= 0) {
      var idx = Math.round((this.pastPts.length - 1) * (1 + this.playT));
      idx = Math.max(0, Math.min(this.pastPts.length - 1, idx));
      var v = this.pastPts[idx].v;
      return { value: v, lo: v, hi: v, future: false };       // the past is known: no band
    }
    var f = this.playT;                                        // 0..1 into horizon
    var mean = this.base + this.drift * f;
    var spread = (0.015 + 0.12 * Math.sqrt(f));                // uncertainty grows with horizon
    return { value: mean, lo: mean - spread, hi: mean + spread, future: true };
  };
  TiktoTick.prototype.emit = function () { this.onScrub(this.sample(), this.playT); };
  TiktoTick.prototype._x = function (t) { return 20 + (t + 1) / 2 * (this.w - 40); }; // t in -1..1
  TiktoTick.prototype._y = function (v) {
    var min = 1.0, max = 1.9; // LCR-ish band for layout
    return this.h - 24 - (v - min) / (max - min) * (this.h - 44);
  };
  TiktoTick.prototype.draw = function () {
    var ctx = this.ctx, w = this.w, h = this.h, self = this;
    if (!ctx) return;
    ctx.clearRect(0, 0, w, h);
    var live = css('--tk-live'), warn = css('--tk-warning'), crit = css('--tk-critical');
    var nowX = this._x(0);

    // regulatory floor line (LCR = 1.00) — the line you must not breach
    var floorY = this._y(1.0);
    ctx.setLineDash([3, 4]); ctx.beginPath(); ctx.moveTo(20, floorY); ctx.lineTo(w - 20, floorY);
    ctx.strokeStyle = hexA(crit, 0.5); ctx.lineWidth = 1; ctx.stroke(); ctx.setLineDash([]);
    ctx.font = '9px "JetBrains Mono", monospace'; ctx.fillStyle = hexA(crit, 0.7); ctx.textAlign = 'left';
    ctx.fillText('FLOOR 1.00×', 22, floorY - 5);

    // past actuals — solid
    ctx.beginPath();
    for (var i = 0; i < this.pastPts.length; i++) {
      var t = -1 + (i / (this.pastPts.length - 1)) * 1;       // -1..0
      var x = this._x(t), y = this._y(this.pastPts[i].v);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.strokeStyle = hexA(css('--tk-text-1'), 0.85); ctx.lineWidth = 1.6; ctx.stroke();

    // future CONE — fan that widens with the horizon
    var steps = 48, top = [], bot = [];
    for (var s = 0; s <= steps; s++) {
      var f = s / steps;                                       // 0..1
      var mean = this.base + this.drift * f;
      var spread = (0.015 + 0.12 * Math.sqrt(f));
      var x2 = this._x(f);
      top.push([x2, this._y(mean + spread)]); bot.push([x2, this._y(mean - spread)]);
    }
    ctx.beginPath(); ctx.moveTo(top[0][0], top[0][1]);
    for (var a = 1; a < top.length; a++) ctx.lineTo(top[a][0], top[a][1]);
    for (var b = bot.length - 1; b >= 0; b--) ctx.lineTo(bot[b][0], bot[b][1]);
    ctx.closePath();
    var grad = ctx.createLinearGradient(nowX, 0, w, 0);
    grad.addColorStop(0, hexA(live, 0.34)); grad.addColorStop(1, hexA(live, 0.04));
    ctx.fillStyle = grad; ctx.fill();
    // mean forecast line (dashed = projected, not actual)
    ctx.setLineDash([4, 3]); ctx.beginPath();
    for (var m = 0; m <= steps; m++) { var fm = m / steps, xm = this._x(fm), ym = this._y(this.base + this.drift * fm); m ? ctx.lineTo(xm, ym) : ctx.moveTo(xm, ym); }
    ctx.strokeStyle = hexA(live, 0.8); ctx.lineWidth = 1.4; ctx.stroke(); ctx.setLineDash([]);

    // NOW line — glowing
    ctx.beginPath(); ctx.moveTo(nowX, 8); ctx.lineTo(nowX, h - 18);
    ctx.strokeStyle = hexA(live, 0.9); ctx.lineWidth = 1.5; ctx.shadowColor = live; ctx.shadowBlur = 10; ctx.stroke(); ctx.shadowBlur = 0;
    ctx.fillStyle = live; ctx.textAlign = 'center'; ctx.fillText('NOW', nowX, h - 4);

    // playhead
    var ph = this._x(this.playT), samp = this.sample(), phY = this._y(samp.value);
    ctx.beginPath(); ctx.moveTo(ph, 8); ctx.lineTo(ph, h - 18);
    ctx.strokeStyle = hexA(samp.future ? warn : css('--tk-text-0'), 0.9); ctx.lineWidth = 1.2; ctx.stroke();
    ctx.beginPath(); ctx.arc(ph, phY, 5, 0, 6.2832);
    ctx.fillStyle = samp.future ? warn : css('--tk-text-0'); ctx.fill();
    ctx.strokeStyle = '#000'; ctx.lineWidth = 1.5; ctx.stroke();
  };
  TiktoTick.prototype._bindDrag = function () {
    var self = this, dragging = false;
    function setFromX(clientX) {
      var r = self.canvas.getBoundingClientRect();
      var t = ((clientX - r.left - 20) / (r.width - 40)) * 2 - 1;
      self.playT = Math.max(-1, Math.min(1, t));
      self.draw(); self.emit();
    }
    this.canvas.addEventListener('pointerdown', function (e) { dragging = true; self.canvas.setPointerCapture(e.pointerId); setFromX(e.clientX); });
    this.canvas.addEventListener('pointermove', function (e) { if (dragging) setFromX(e.clientX); });
    this.canvas.addEventListener('pointerup', function () { dragging = false; });
    // keyboard: arrow keys scrub time (expert-speed, accessible)
    this.canvas.tabIndex = 0;
    this.canvas.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowRight') { self.playT = Math.min(1, self.playT + 0.05); self.draw(); self.emit(); e.preventDefault(); }
      if (e.key === 'ArrowLeft') { self.playT = Math.max(-1, self.playT - 0.05); self.draw(); self.emit(); e.preventDefault(); }
    });
  };

  /* ======================= NUMBER ROLL ==================================== */
  /* Animates only when the value actually changed. fmt(v)->string. */
  function tiktoRoll(el, to, fmt, dur) {
    fmt = fmt || function (v) { return v.toFixed(2); };
    var from = parseFloat(el.getAttribute('data-tk-val'));
    if (isNaN(from)) from = to;
    el.setAttribute('data-tk-val', to);
    if (REDUCED || from === to) { el.textContent = fmt(to); return; }
    dur = dur || 380; var t0 = performance.now();
    function step(now) {
      var k = Math.min(1, (now - t0) / dur);
      k = 1 - Math.pow(1 - k, 3); // easeOutCubic
      el.textContent = fmt(from + (to - from) * k);
      if (k < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  global.Tikto = { Network: TiktoNetwork, Tick: TiktoTick, roll: tiktoRoll, hexA: hexA, css: css };
})(window);
