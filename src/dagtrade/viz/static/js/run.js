/* A backtest result: tiles, scorecard, equity, price chart with trades + node panes, DAG, trades table */
/* ----- run detail ----- */
const hidden = new Set();   // series ids the user toggled
/* The full set of result sections (used by backtest pages AND by each live DAG page). */
function runSections(P, { heading = true } = {}) {
  const { meta, trades, chart } = P;
  const m = meta.metrics, intraday = !['1d', '1w'].includes(meta.base_tf);
  const out = [];
  if (heading) {
    out.push(h('div', { class: 'page-head' },
      h('div', {}, h('h1', {}, STATIC ? meta.strategy : h('a', { href: '#/dag/' + encodeURIComponent(meta.strategy) }, meta.strategy), ` on ${meta.ticker}`),
        h('div', { class: 'meta-row' }, h('span', { class: 'pill' }, meta.base_tf + ' candles'),
          meta.flat_by_close ? h('span', { class: 'pill info', title: 'Day trade: no new entries in the last 30 min, out before 16:00 ET' }, 'flat by close') : null,
          h('span', { class: 'muted' }, `${meta.start} → ${meta.end}`),
          meta.causal && /OK/.test(meta.causal) ? h('span', { class: 'pill good', title: meta.causal }, '✓ lookahead-checked') : null)),
      ), h('p', { class: 'sub' }, meta.description || ''));
  }
  if (meta.insights && meta.insights.length)
    out.push(h('div', { class: 'card notes' }, h('h2', {}, 'Read this first'), h('ul', {}, meta.insights.map((i) => h('li', {}, i)))));
  out.push(tiles(m, meta), scorecard(m, meta), equityCard(chart, meta), priceCard(chart, meta, intraday, trades),
    dagCard(meta), meta.events ? transitionsCard(meta) : null, tradesCard(trades, chart), ordersCard(P.orders || [], 'simulated fills (the source of every number above)'));
  return out;
}

async function renderRun(P) {
  const { meta } = P;
  $('#crumb').textContent = `${meta.strategy} · ${meta.ticker}`;
  if (STATIC) $('#brand').removeAttribute('href');
  app.replaceChildren(h('div', {}, ...runSections(P)));
}

function tiles(m, meta) {
  const s = m.strategy, bh = m.buy_hold || {}, sp = m.spy || {}, t = m.trades;
  const tile = (k, v, d, c) => h('div', { class: 'tile' }, h('div', { class: 'k' }, k), h('div', { class: 'v ' + (c || '') }, v), h('div', { class: 'd' }, d || ' '));
  return h('div', { class: 'tiles' },
    tile('Return', pct(s.total_return), `${meta.ticker} buy&hold ${pct(bh.total_return)} · SPY ${pct(sp.total_return)}`, cls(s.total_return)),
    tile('CAGR', pct(s.cagr), `B&H ${pct(bh.cagr)} · SPY ${pct(sp.cagr)}`, cls(s.cagr)),
    tile('Sharpe', num(s.sharpe), `B&H ${num(bh.sharpe)} · SPY ${num(sp.sharpe)}`),
    tile('Max drawdown', pct(s.max_drawdown, 1), `B&H ${pct(bh.max_drawdown, 0)} · SPY ${pct(sp.max_drawdown, 0)}`, 'neg'),
    tile('Trades', t.n_trades, `win ${pctp(t.win_rate, 0)} · PF ${num(t.profit_factor)}`),
    tile('Time in market', pctp(t.exposure, 0), `avg hold ${num(t.avg_bars_held, 1)} bars`));
}

function scorecard(m, meta) {
  const rowsDef = [['Total return', 'total_return', pct], ['CAGR', 'cagr', pct], ['Volatility', 'vol', pctp], ['Sharpe', 'sharpe', num],
    ['Sortino', 'sortino', num], ['Max drawdown', 'max_drawdown', pct], ['Calmar', 'calmar', num], ['Worst day', 'worst_day', pct]];
  const heads = ['Strategy', `${meta.ticker} buy&hold`, meta.benchmark];
  const keys = ['strategy', 'buy_hold', 'spy'];
  const t1 = h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, ''), heads.map((x) => h('th', {}, x)))),
    h('tbody', {}, rowsDef.map(([l, k, f]) => h('tr', {}, h('td', {}, l), keys.map((kk) => h('td', {}, m[kk] && m[kk][k] != null ? f(m[kk][k]) : '–'))))));
  const rel = m.vs_spy || {};
  const extra = h('p', { class: 'note' }, rel.beta != null ? `Versus ${meta.benchmark}: beta ${num(rel.beta)} · alpha ${pct(rel.alpha)}/yr · correlation ${num(rel.corr)} · excess return ${pct(rel.excess_return)}` : '');
  const card1 = h('div', { class: 'card' }, h('h2', {}, 'Scorecard'), h('div', { class: 'scroll' }, t1), extra);
  const sp = m.split;
  let card2 = h('div', { class: 'card' }, h('h2', {}, 'Overfitting & luck checks'), h('p', { class: 'muted' }, 'No holdout split for this run (live charts and very short runs skip it).'));
  if (sp && sp.in_sample) {
    const a = sp.in_sample, b = sp.holdout;
    const r = (l, k, f) => h('tr', {}, h('td', {}, l), h('td', {}, a[k] != null ? f(a[k]) : '–'), h('td', {}, b[k] != null ? f(b[k]) : '–'));
    card2 = h('div', { class: 'card' }, h('h2', {}, 'Overfitting & luck checks'),
      h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, ''), h('th', {}, 'In-sample'), h('th', {}, `Holdout (from ${sp.split_date})`))),
        h('tbody', {}, r('Return', 'total_return', pct), r('Sharpe', 'sharpe', num), r('Max drawdown', 'max_drawdown', pct), r('Trades', 'n_trades', (x) => x), r('Win rate', 'win_rate', (x) => pctp(x, 0)))),
      h('p', { class: 'note' }, 'The holdout is the most recent slice of the period. If results collapse there, the strategy was probably fitted to noise.'),
      m.baseline ? h('p', { class: 'note' }, `Random-timing test: same exposure at random times beat this strategy ${(100 - m.baseline.percentile).toFixed(0)}% of the time (median random run ${pct(m.baseline.random_median)}, 5–95% range ${pct(m.baseline.random_p05, 0)} to ${pct(m.baseline.random_p95, 0)}).`) : null);
  }
  return h('div', { class: 'row' }, card1, card2);
}


function equityCard(chart, meta) {
  const e = chart.equity;
  const defs = [['strategy', 'Strategy', css('--s1'), 2.4], ['buy_hold', meta.ticker + ' buy&hold', css('--s2'), 1.6], ['spy', meta.benchmark, css('--s3'), 1.6]];
  const lg = h('div', { class: 'legend' });
  const box = h('div', { class: 'chart' }), ddBox = h('div', { class: 'chart' });
  const card = h('div', { class: 'card' }, h('h2', {}, 'Growth of $ (log scale) vs the market'), lg, box, h('h2', { style: 'margin-top:12px' }, 'Strategy drawdown'), ddBox);
  queueMicrotask(() => {
    const c = mkChart(box, 300, false, { rightPriceScale: { borderColor: css('--border'), mode: LightweightCharts.PriceScaleMode.Logarithmic }, localization: { priceFormatter: (p) => '$' + Math.round(p).toLocaleString() } });
    const ser = [];
    const vals = {};
    for (const [k, label, color, w] of defs) {
      if (!e[k]) continue;
      const s = c.addLineSeries({ color, lineWidth: w, priceLineVisible: false, lastValueVisible: false });
      s.setData(toPts(e.t, e[k])); ser.push([k, s]);
      const sp = h('span', { class: 'val' }, '');
      vals[k] = sp;
      lg.append(h('span', {}, h('i', { style: `background:${color}` }), label, sp));
    }
    c.timeScale().fitContent();
    const d = mkChart(ddBox, 130, false, { localization: { priceFormatter: (p) => (p * 100).toFixed(0) + '%' } });
    const ds = d.addAreaSeries({ lineColor: css('--bad'), topColor: 'transparent', bottomColor: css('--bad') + '55', lineWidth: 1, priceLineVisible: false, invertFilter: true });
    ds.setData(toPts(e.t, e.drawdown));
    d.priceScale('right').applyOptions({ scaleMargins: { top: 0.05, bottom: 0.05 } });
    const dmap = new Map(e.t.map((t, i) => [t, e.drawdown[i]]));
    const emap = new Map(e.t.map((t, i) => [t, e.strategy[i]]));
    linkCharts([{ chart: c, series: ser[0][1], map: emap }, { chart: d, series: ds, map: dmap }]);
    c.subscribeCrosshairMove((p) => ser.forEach(([k, s]) => { const v = p.seriesData && p.seriesData.get(s); vals[k].textContent = v ? '$' + Math.round(v.value).toLocaleString() : ''; }));
  });
  return card;
}


/* ---- price chart with trades + DAG node panes ---- */
let priceCtl = null;
function priceCard(chart, meta, intraday, trades) {
  const overlays = Object.entries(chart.overlays), panes = Object.entries(chart.panes);
  const all = [...overlays, ...panes];
  if (!priceCard.init) {         // defaults: a few named nodes visible; intermediate logic hidden until asked for
    let nOv = 0, nPane = 0;
    all.forEach(([k, s]) => { const isOv = k in chart.overlays;
      if (!s.main || (isOv && nOv >= 3) || (!isOv && nPane >= 4)) hidden.add(k); else isOv ? nOv++ : nPane++; });
    priceCard.init = true;
  }
  const colors = seriesColors();
  const chips = h('div', { class: 'chips' });
  const host = h('div', {});
  const card = h('div', { class: 'card' }, h('h2', {}, 'Price, trades and what each node saw'),
    h('p', { class: 'note', style: 'margin:-6px 0 8px' }, 'Each trade is a box from its entry (one corner) to its exit (the opposite corner): green = profit, red = loss. ▲▼ entry · ● exit. Opens on the most recent candles: scroll or pinch to zoom out, click a trade in the table to jump to it. Toggle node outputs below to stack them under the chart.'),
    chips, host);
  const inter = h('details', {}, h('summary', { class: 'note' }, `intermediate logic nodes (${all.filter(([, s]) => !s.main).length}) - comparisons, gates, combined conditions`));
  const interChips = h('div', { class: 'chips' });
  inter.append(interChips);
  all.forEach(([k, s], i) => {
    const chip = h('span', { class: 'chip' + (hidden.has(k) ? '' : ' on'), 'data-k': k, title: s.node, onclick: () => { hidden.has(k) ? hidden.delete(k) : hidden.add(k); drawAll(); chip.classList.toggle('on'); } },
      h('i', { style: `background:${colors[i % colors.length]}` }), k);
    (s.main ? chips : interChips).append(chip);
  });
  if (all.some(([, s]) => !s.main)) card.insertBefore(inter, host);
  function drawAll() {
    charts.filter((c) => c.__price).forEach((c) => { try { c.remove(); } catch (e) {} });
    charts = charts.filter((c) => !c.__price);
    host.replaceChildren();
    const mainBox = h('div', { class: 'chart' });
    host.append(mainBox);
    const main = mkChart(mainBox, 440, intraday); main.__price = true;
    const cs = main.addCandlestickSeries({ upColor: css('--up'), downColor: css('--down'), wickUpColor: css('--up'), wickDownColor: css('--down'), borderVisible: false });
    const t = chart.t;
    cs.setData(t.map((tt, i) => ({ time: tt, open: chart.o[i], high: chart.h[i], low: chart.l[i], close: chart.c[i] })));
    const ms = chart.markers.map((mk) => {
      if (mk.kind === 'entry') return { time: mk.time, position: mk.side === 'long' ? 'belowBar' : 'aboveBar', color: css('--s1'), shape: mk.side === 'long' ? 'arrowUp' : 'arrowDown', text: mk.text };
      return { time: mk.time, position: mk.side === 'long' ? 'aboveBar' : 'belowBar', color: mk.win ? css('--good') : css('--bad'), shape: 'circle', text: mk.text };
    });
    (chart.live_fills || []).forEach((f) => ms.push({ time: f.time, position: f.side === 'buy' ? 'belowBar' : 'aboveBar', color: css('--s7'), shape: 'square', text: `paper ${f.side} ${f.qty} @ ${f.price.toFixed(2)}` }));
    ms.sort((a, b) => a.time - b.time);
    cs.setMarkers(ms);
    if (trades.length) cs.attachPrimitive(new TradeBoxes(trades));
    overlays.forEach(([k, s]) => {
      if (hidden.has(k)) return;
      const ls = main.addLineSeries({ color: colors[all.findIndex(([kk]) => kk === k) % colors.length], lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, title: k,
        autoscaleInfoProvider: () => null });   // overlays never stretch the price axis (e.g. a far-away take-profit)
      ls.setData(gappedLine(t, s.v));                 // gaps stay gaps (e.g. no stop while flat)
    });
    const cmap = new Map(t.map((tt, i) => [tt, chart.c[i]]));
    const items = [{ chart: main, series: cs, map: cmap }];
    const legendMain = h('div', { class: 'tag' }, '');
    mainBox.append(legendMain);
    main.subscribeCrosshairMove((p) => { const d = p.seriesData && p.seriesData.get(cs); legendMain.textContent = d ? `${when(p.time)}  O ${d.open}  H ${d.high}  L ${d.low}  C ${d.close}` : ''; });
    panes.forEach(([k, s]) => {
      if (hidden.has(k)) return;
      const box = h('div', { class: 'pane' });
      const tag = h('div', { class: 'tag' }, k);
      const inner = h('div', { class: 'chart' });
      box.append(tag, inner); host.append(box);
      const c = mkChart(inner, 120, intraday, { timeScale: { ...baseOptions(intraday).timeScale, visible: false } }); c.__price = true;
      const col = colors[all.findIndex(([kk]) => kk === k) % colors.length];
      let ser;
      let labelAt = null;
      if (s.kind === 'cat') {              // regime labels: one colour per label
        const pal = seriesColors();
        ser = c.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 1 } }) });
        ser.setData(t.map((tt, i) => (s.v[i] == null ? { time: tt } : { time: tt, value: 1, color: pal[s.v[i] % pal.length] })));
        labelAt = new Map(t.map((tt, i) => [tt, s.v[i] == null ? null : s.labels[s.v[i]]]));
        tag.innerHTML = k + '  ' + s.labels.map((l, j) => `<span style="color:${pal[j % pal.length]}">■</span> ${l}`).join('  ');
      } else if (s.kind === 'bool') {
        ser = c.addHistogramSeries({ color: col, priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 1 } }) });
        ser.setData(t.map((tt, i) => (s.v[i] == null ? { time: tt } : { time: tt, value: s.v[i], color: s.v[i] ? col : 'transparent' })));
      } else {
        ser = c.addLineSeries({ color: col, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true });
        ser.setData(toPts(t, s.v));
      }
      const map = new Map(t.map((tt, i) => [tt, s.v[i]]));
      c.subscribeCrosshairMove((p) => {
        if (labelAt) { const pal2 = seriesColors(); const lab = p.time !== undefined ? labelAt.get(p.time) : null; if (lab) tag.textContent = k + '  =  ' + lab; else tag.innerHTML = k + '  ' + s.labels.map((l, j) => `<span style="color:${pal2[j % pal2.length]}">■</span> ${l}`).join('  '); return; }
        const d = p.seriesData && p.seriesData.get(ser); tag.textContent = k + (d && d.value != null ? '  =  ' + (s.kind === 'bool' ? (d.value ? 'true' : 'false') : Number(d.value).toPrecision(5)) : '');
      });
      items.push({ chart: c, series: ser, map });
    });
    // the bottom-most chart shows the time axis
    const lastBox = host.lastElementChild;
    if (items.length > 1) items[items.length - 1].chart.applyOptions({ timeScale: { visible: true } });
    main.timeScale().fitContent();
    if (t.length > 400) main.timeScale().setVisibleLogicalRange({ from: t.length - 260, to: t.length + 3 });   // open on the last ~year; scroll/pinch to zoom out
    linkCharts(items);
    priceCtl = { main, cs, t, tradeLines: [] };
  }
  card.__draw = drawAll;
  queueMicrotask(drawAll);
  priceCard.focus = (tr) => {
    if (!priceCtl) return;
    const { main, cs, t } = priceCtl;
    priceCtl.tradeLines.forEach((l) => cs.removePriceLine(l)); priceCtl.tradeLines = [];
    const i0 = Math.max(0, lowerBound(t, tr.entry_time) - 40), i1 = Math.min(t.length - 1, lowerBound(t, tr.exit_time) + 40);
    main.timeScale().setVisibleRange({ from: t[i0], to: t[i1] });
    const mk = (price, title, color) => priceCtl.tradeLines.push(cs.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, title }));
    mk(tr.entry_price, 'entry', css('--s1')); mk(tr.exit_price, 'exit ' + tr.exit_reason, tr.pnl > 0 ? css('--good') : css('--bad'));
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  return card;
}

/* ---- DAG diagram ---- */
/* ---- the strategy as a state machine: states left to right; "ok" to the next state is a straight arrow; every
   other edge (fail / max_wait / named outcomes / the loop back to the start) is an arc, backwards ones underneath.
   Dashed = a default edge (not written in the strategy file). Old DAG runs are drawn the same way. ---- */
function fsaEdges(dag) {
  if (dag.edges) return dag.edges;
  const e = [];
  dag.nodes.forEach((n) => (n.inputs || []).forEach((i) => e.push({ from: i, to: n.id, outcome: 'ok', default: false })));
  return e;
}

function fsaDiagram(dag, { active = null, done = [], onNode = null, plotted = new Set() } = {}) {
  const nodes = dag.nodes, exits = dag.exits || [];
  const W = 176, H = 54, GX = 70, TOP = 40;
  const idx = Object.fromEntries(nodes.map((n, i) => [n.id, i]));
  const X = (i) => 12 + i * (W + GX);
  const groups = {};
  fsaEdges(dag).forEach((e) => {
    if (!(e.from in idx) || !(e.to in idx)) return;
    if (e.from === e.to && e.default) return;                           // a start state "failing" back to itself
    const k = e.from + '>' + e.to;
    (groups[k] = groups[k] || { from: e.from, to: e.to, outs: [], dflt: true }).outs.push(e.outcome);
    if (!e.default) groups[k].dflt = false;
  });
  const arcs = Object.values(groups);
  let below = 0, above = 0;
  arcs.forEach((a) => { const d = idx[a.to] - idx[a.from]; if (d <= 0) below = Math.max(below, 30 + 16 * Math.abs(d)); else if (d > 1) above = Math.max(above, 24 + 12 * d); });
  const top = Math.max(TOP, above + 14), exitY = top + H + below + 24;
  const width = X(nodes.length - 1) + W + 14, height = (exits.length ? exitY + 44 : top + H + below + 12);
  const NS = 'http://www.w3.org/2000/svg';
  const el = (n, a, txt) => { const e = document.createElementNS(NS, n); for (const [k, v] of Object.entries(a || {})) e.setAttribute(k, v); if (txt != null) e.textContent = txt; return e; };
  const svg = el('svg', { width, height, viewBox: `0 0 ${width} ${height}` });
  const defs = el('defs');
  [['arr', 'arrow'], ['arrd', 'arrow dflt']].forEach(([id, c]) => {
    const m = el('marker', { id: id + dag.start, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' });
    m.append(el('path', { d: 'M0,0 L10,5 L0,10 z', class: c })); defs.append(m);
  });
  svg.append(defs);
  arcs.forEach((a) => {
    const i = idx[a.from], k = idx[a.to], d = k - i, label = a.outs.join(' / ');
    const cls = 'edge' + (a.dflt ? ' dflt' : '') + (a.outs.some((o) => o !== 'ok') ? ' alt' : '');
    const mk = `url(#${a.dflt ? 'arrd' : 'arr'}${dag.start})`;
    let path, lx, ly;
    if (d === 1) {
      const y = top + H / 2; path = `M${X(i) + W},${y} L${X(k) - 2},${y}`; lx = (X(i) + W + X(k)) / 2; ly = y - 6;
    } else if (d <= 0) {
      const x1 = X(i) + W / 2 + (d === 0 ? 30 : 10), x2 = X(k) + W / 2 - (d === 0 ? 30 : 10), y = top + H, depth = 30 + 16 * Math.abs(d);
      path = `M${x1},${y} C${x1},${y + depth} ${x2},${y + depth} ${x2},${y + 2}`; lx = (x1 + x2) / 2; ly = y + depth * 0.75 + 4;
    } else {
      const x1 = X(i) + W / 2 + 10, x2 = X(k) + W / 2 - 10, y = top, h = 24 + 12 * d;
      path = `M${x1},${y} C${x1},${y - h} ${x2},${y - h} ${x2},${y - 2}`; lx = (x1 + x2) / 2; ly = y - h * 0.75 - 3;
    }
    svg.append(el('path', { d: path, class: cls, 'marker-end': mk }));
    if (!(d === 1 && label === 'ok')) svg.append(el('text', { x: lx, y: ly, class: 'edge-label', 'text-anchor': 'middle' }, label));
  });
  const box = (n, x, y, w, h, extra) => {
    const g = el('g', { class: 'node', transform: `translate(${x},${y})` });
    const st = n.id === active ? ' active' : done.includes(n.id) ? ' done' : '';
    g.append(el('rect', { class: 'box ' + (extra || '') + st + (plotted.has(n.id) ? ' on' : ''), width: w, height: h, rx: 8 }));
    g.append(el('text', { x: 10, y: 19 }, (n.id.length > 24 ? n.id.slice(0, 23) + '…' : n.id)));
    const sub = `${n.kind} · ${n.agnostic && !dag._run ? 'any interval' : n.tf}` + (n.max_wait ? ` · wait ≤ ${n.max_wait.replace(' candles', '')}` : '');
    g.append(el('text', { class: 'sm', x: 10, y: 34 }, sub));
    const ps = Object.entries(n.params || {}).filter(([, v]) => v !== null).map(([k, v]) => `${k}=${v}`).join(' ');
    if (ps && h > 40) g.append(el('text', { class: 'sm', x: 10, y: 47 }, ps.length > 30 ? ps.slice(0, 29) + '…' : ps));
    if (n.start) { g.append(el('text', { class: 'badge', x: w - 8, y: -5, 'text-anchor': 'end' }, '▶ start')); }
    g.append(el('title', {}, `${n.id}\n${n.type || ''}\nreads ${n.history || ''}\n${ps}`));
    if (onNode) g.addEventListener('click', () => onNode(n.id));
    svg.append(g);
  };
  nodes.forEach((n, i) => box(n, X(i), top, W, H));
  exits.forEach((x) => {
    const ti = idx[x.exit_of]; if (ti == null) return;
    const ex = X(ti) + 10, ey = exitY;
    svg.append(el('path', { d: `M${X(ti) + W / 2},${top + H} L${X(ti) + W / 2},${ey}`, class: 'edge dflt' }));
    box({ ...x, kind: 'exit check', start: false, max_wait: null }, ex, ey, W - 20, 40, 'exit');
  });
  return h('div', { class: 'dag' }, svg);
}

function dagCard(meta) {
  const dag = { ...meta.dag, _run: !!meta.id };
  const plotted = new Set(Object.values(meta.node_info || {}).map((i) => i.node));
  const onNode = (id) => {
    const ks = Object.entries(meta.node_info || {}).filter(([, i]) => i.node === id).map(([k]) => k);
    if (!ks.length || !document.querySelector('.chips')) return;
    const allHidden = ks.every((k) => hidden.has(k));
    ks.forEach((k) => (allHidden ? hidden.delete(k) : hidden.add(k)));
    const pc = document.querySelector('.chips').closest('.card');
    document.querySelectorAll('.chips .chip').forEach((c) => c.classList.toggle('on', !hidden.has(c.dataset.k)));
    pc.__draw(); pc.scrollIntoView({ behavior: 'smooth' });
  };
  const isFsa = !!dag.edges;
  return h('div', { class: 'card' }, h('h2', {}, isFsa ? `Strategy · ${dag.nodes.length} states` : `Strategy DAG · ${dag.nodes.length} nodes`),
    h('p', { class: 'note', style: 'margin:-6px 0 8px' }, isFsa
      ? 'A state machine: the active state waits for its condition, then moves along "ok" (straight arrows). Arcs are the other outcomes - fail, max_wait (waited too long) or a named branch; dashed ones are defaults (back to the start). After the Trade closes its position it loops back to the start. Click a state to show/hide its output on the chart.'
      : 'Data flows left → right. Click a node to show/hide its output on the chart.'),
    fsaDiagram(dag, { onNode, plotted }));
}

/* ---- how the state machine moved during the backtest ---- */
function transitionsCard(meta) {
  const ev = meta.events || [];
  const counts = {};
  ev.forEach((e) => { const k = `${e.from} → ${e.to}|${e.outcome}`; counts[k] = (counts[k] || 0) + 1; });
  const rows = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const last = ev.slice(-60).reverse();
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, 'State transitions ', h('span', { class: 'muted' }, String(ev.length))),
      h('span', { class: 'muted small' }, 'how often each edge was taken; the newest moves below')),
    h('div', { class: 'kv', style: 'margin-bottom:10px' }, rows.map(([k, n]) => { const [edge, out] = k.split('|'); return h('span', { class: out === 'ok' ? '' : 'on' }, `${edge} (${out}) × ${n}`); })),
    h('details', {}, h('summary', { class: 'small muted' }, `last ${last.length} transitions`),
      h('div', { class: 'scroll', style: 'max-height:280px;overflow:auto' }, h('table', { class: 'tbl' },
        h('thead', {}, h('tr', {}, ['Time', 'From', 'To', 'Outcome', 'Why'].map((t) => h('th', { class: 'l' }, t)))),
        h('tbody', {}, last.map((e) => h('tr', {}, h('td', { class: 'l small nowrap' }, isoTime(new Date(e.t * 1000).toISOString())),
          h('td', { class: 'l' }, e.from), h('td', { class: 'l' }, e.to), h('td', { class: 'l' }, e.outcome), h('td', { class: 'l small muted' }, e.reason || ''))))))));
}

/* ---- trades ---- */
function tradesCard(trades, chart, title = 'Trades') {
  const cols = [['entry_time', 'Entry', (r) => when(r.entry_time), 'l'], ['exit_time', 'Exit', (r) => r.open ? h('span', { class: 'pill info sm' }, 'open') : when(r.exit_time), 'l'], ['side', 'Side', (r) => r.side, 'l'],
    ['entry_price', 'Entry px', (r) => num(r.entry_price)], ['exit_price', 'Exit px', (r) => num(r.exit_price)],
    ['ret', 'Return', (r) => h('span', { class: cls(r.pnl) }, pct(r.ret, 2))], ['pnl', 'P&L $', (r) => h('span', { class: cls(r.pnl) }, num(r.pnl, 2))],
    ['bars_held', 'Bars', (r) => r.bars_held], ['exit_reason', 'Exit by', (r) => r.exit_reason, 'l'], ['mae', 'Worst dip', (r) => pct(r.mae, 1)], ['mfe', 'Best run-up', (r) => pct(r.mfe, 1)]];
  let key = 'entry_time', dir = 1;
  const wrap = h('div', { class: 'scroll', style: 'max-height:480px;overflow:auto' });
  const draw = () => {
    const rows = trades.slice().sort((a, b) => (a[key] > b[key] ? 1 : -1) * dir);
    wrap.replaceChildren(h('table', {}, h('thead', {}, h('tr', {}, cols.map(([k, t, , c]) => h('th', { class: 'sortable ' + (c || ''), onclick: () => { dir = key === k ? -dir : 1; key = k; draw(); } }, t + (key === k ? (dir > 0 ? ' ▲' : ' ▼') : ''))))),
      h('tbody', {}, rows.map((r) => h('tr', { class: 'click', onclick: () => priceCard.focus(r) }, cols.map(([, , f, c]) => h('td', { class: c || '' }, f(r))))))));
  };
  draw();
  return h('div', { class: 'card' }, h('h2', {}, `${title} (${trades.length})`), h('p', { class: 'note', style: 'margin:-6px 0 8px' }, 'Click a trade to jump the chart to it.'), trades.length ? wrap : h('p', { class: 'muted' }, 'No trades were taken.'));
}


/* Orders / fills - the raw source every result is computed from. rows: {submitted, side, qty, type, stop, limit,
   status, fill_price, reason?} */
function ordersCard(rows, where) {
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, 'Orders ', h('span', { class: 'muted' }, String(rows.length))), h('span', { class: 'muted small' }, where || '')),
    rows.length ? h('div', { class: 'scroll', style: 'max-height:360px;overflow:auto' }, h('table', { class: 'tbl' },
      h('thead', {}, h('tr', {}, ['Time', 'Side', 'Qty', 'Type', 'Price', 'Status', 'Filled at', 'Fee'].map((t, i) => h('th', { class: i < 2 || i === 3 || i === 5 ? 'l' : '' }, t)))),
      h('tbody', {}, rows.map((o) => h('tr', {},
        h('td', { class: 'l small' }, typeof o.submitted === 'number' ? when(o.submitted) : isoTime(o.submitted)),
        h('td', { class: 'l' }, h('span', { class: o.side === 'buy' ? 'pos' : 'neg' }, o.side)),
        h('td', {}, Number(o.qty).toFixed(Number.isInteger(o.qty) ? 0 : 3)),
        h('td', { class: 'l small' }, o.reason ? `${o.type} (${o.reason})` : o.type),
        h('td', { class: 'small' }, o.type === 'stop' ? 'stop ' + money(o.stop) : o.limit ? 'limit ' + money(o.limit) : 'market'),
        h('td', { class: 'l' }, h('span', { class: 'pill sm ' + ({ filled: 'good', canceled: 'muted', expired: 'muted', rejected: 'bad' }[o.status] || 'info') }, o.status)),
        h('td', {}, o.fill_price ? money(o.fill_price) : '–'),
        h('td', { class: 'small muted' }, o.fee != null ? money(o.fee) : '–')))))) : h('p', { class: 'muted' }, 'No orders yet.'));
}
