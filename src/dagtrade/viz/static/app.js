/* dagtrade dashboard - vanilla JS + TradingView Lightweight Charts v4 */
(() => {
const $ = (s, el = document) => el.querySelector(s);
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const STATIC = window.STATIC_RUN || null;
if (STATIC) { const n = document.querySelector('nav'); if (n) n.style.display = 'none'; }

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) el.append(c.nodeType ? c : document.createTextNode(c));
  return el;
}
const pct = (x, d = 1) => x == null ? '–' : (x >= 0 ? '+' : '') + (x * 100).toFixed(d) + '%';
const pctp = (x, d = 1) => x == null ? '–' : (x * 100).toFixed(d) + '%';
const num = (x, d = 2) => x == null ? '–' : Number(x).toFixed(d);
const cls = (x) => x == null ? '' : x >= 0 ? 'pos' : 'neg';
const when = (s) => new Date(s * 1000).toISOString().replace('T', ' ').slice(0, 16);

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

/* ---------- theme ---------- */
$('#theme').addEventListener('click', () => {
  const cur = document.documentElement.dataset.theme ||
    (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const nxt = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = nxt;
  try { localStorage.setItem('theme', nxt); } catch (e) {}
  render();
});
try { const t = localStorage.getItem('theme'); if (t) document.documentElement.dataset.theme = t; } catch (e) {}

/* ---------- charts ---------- */
let charts = [];
function disposeCharts() { charts.forEach((c) => { try { c.remove(); } catch (e) {} }); charts = []; }
function baseOptions(intraday) {
  return {
    layout: { background: { type: 'solid', color: css('--surface') }, textColor: css('--text-2'), fontSize: 11 },
    grid: { vertLines: { color: css('--grid') }, horzLines: { color: css('--grid') } },
    rightPriceScale: { borderColor: css('--border') },
    timeScale: { borderColor: css('--border'), timeVisible: intraday, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    autoSize: true,
  };
}
function mkChart(el, height, intraday, extra) {
  el.style.height = height + 'px';
  const c = LightweightCharts.createChart(el, { ...baseOptions(intraday), ...(extra || {}) });
  charts.push(c);
  return c;
}
const seriesColors = () => ['--s2', '--s3', '--s5', '--s4', '--s7', '--s6', '--s8', '--s1'].map(css);

/* Link time ranges + crosshairs of several charts: items = [{chart, series, map}] */
function linkCharts(items) {
  let lock = false;
  items.forEach((a) => {
    a.chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (lock || !r) return;
      lock = true;
      items.forEach((b) => { if (b !== a) b.chart.timeScale().setVisibleLogicalRange(r); });
      lock = false;
    });
    a.chart.subscribeCrosshairMove((p) => {
      items.forEach((b) => {
        if (b === a) return;
        if (p.time === undefined) b.chart.clearCrosshairPosition();
        else { const v = b.map ? b.map.get(p.time) : 0; b.chart.setCrosshairPosition(v == null ? 0 : v, p.time, b.series); }
      });
    });
  });
}

/* ---------- views ---------- */
const app = $('#app');
let current = null;

let livePoll = null;
async function render() {
  disposeCharts();
  if (livePoll) { clearInterval(livePoll); livePoll = null; }
  const hash = location.hash || '#/';
  $('#crumb').textContent = '';
  try {
    if (STATIC) return renderRun(STATIC);
    const m = hash.match(/^#\/run\/(.+)$/);
    if (m) return renderRun(await api('/api/run/' + encodeURIComponent(m[1])));
    if (hash === '#/live') return await renderLive();
    return await renderHome();
  } catch (e) {
    app.replaceChildren(h('div', { class: 'card err' }, 'Error: ' + e.message));
  }
}
window.addEventListener('hashchange', render);

/* ----- home: leaderboard ----- */
let sortKey = 'created', sortDir = -1, latestOnly = true;
async function renderHome() {
  const [runs, deps] = await Promise.all([api('/api/runs'), api('/api/deployments')]);
  const sel = h('select', {}, deps.map((d) => h('option', { value: d.label }, `${d.label}  (from ${d.start})`)));
  const status = h('span', { class: 'muted' });
  const runBtn = h('button', { class: 'primary', onclick: async () => {
    runBtn.disabled = true; status.textContent = 'running… (fetching data on first run can take a minute)';
    try { const r = await api('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label: sel.value }) });
      location.hash = '#/run/' + r.id; } catch (e) { status.className = 'err'; status.textContent = e.message; runBtn.disabled = false; }
  } }, 'Run backtest');
  const cols = [
    ['strategy', 'Strategy', (r) => r.strategy, 'l'], ['ticker', 'Ticker', (r) => r.ticker, 'l'], ['base_tf', 'TF', (r) => r.base_tf],
    ['total_return', 'Return', (r) => h('span', { class: cls(r.total_return) }, pct(r.total_return))],
    ['bh_return', 'Buy&hold', (r) => pct(r.bh_return)], ['spy_return', 'SPY', (r) => pct(r.spy_return)],
    ['sharpe', 'Sharpe', (r) => num(r.sharpe)], ['max_drawdown', 'Max DD', (r) => pct(r.max_drawdown, 0)],
    ['n_trades', 'Trades', (r) => r.n_trades], ['win_rate', 'Win', (r) => pctp(r.win_rate, 0)],
    ['holdout_sharpe', 'Holdout Sharpe', (r) => num(r.holdout_sharpe)],
    ['luck_percentile', 'Beat random %', (r) => r.luck_percentile == null ? '–' : r.luck_percentile.toFixed(0)],
    ['created', 'Run', (r) => r.created.slice(0, 16).replace('T', ' ')],
  ];
  const wrap = h('div', { class: 'scroll' });
  const draw = () => {
    let rows = runs.filter((r) => !r.id.startsWith('live_'));
    if (latestOnly) { const seen = new Set(); rows = rows.filter((r) => { const k = r.strategy + '/' + r.ticker; if (seen.has(k)) return false; seen.add(k); return true; }); }
    rows.sort((a, b) => ((a[sortKey] ?? -1e18) > (b[sortKey] ?? -1e18) ? 1 : -1) * sortDir);
    wrap.replaceChildren(h('table', {},
      h('thead', {}, h('tr', {}, cols.map(([k, t, , c]) => h('th', { class: 'sortable ' + (c || ''), onclick: () => { sortDir = sortKey === k ? -sortDir : -1; sortKey = k; draw(); } }, t + (sortKey === k ? (sortDir > 0 ? ' ▲' : ' ▼') : ''))))),
      h('tbody', {}, rows.map((r) => h('tr', { class: 'click', onclick: () => (location.hash = '#/run/' + r.id) }, cols.map(([, , f, c]) => h('td', { class: c || '' }, f(r))))))));
  };
  draw();
  app.replaceChildren(
    h('h1', {}, 'Backtests'),
    h('p', { class: 'sub' }, 'Every strategy × ticker you have run. Click a row for the chart, the DAG and every trade.'),
    h('div', { class: 'card' }, h('h2', {}, 'Run a deployment'), h('div', { class: 'row', style: 'align-items:center' }, h('div', { style: 'flex:0 0 auto;display:flex;gap:10px;align-items:center;flex-wrap:wrap' }, sel, runBtn, status))),
    h('div', { class: 'card' },
      h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px' }, h('h2', { style: 'margin:0' }, `Runs (${runs.filter((r) => !r.id.startsWith('live_')).length})`),
        h('label', { class: 'muted' }, h('input', { type: 'checkbox', checked: latestOnly, onchange: (e) => { latestOnly = e.target.checked; draw(); } }), ' latest per strategy×ticker only')),
      runs.length ? wrap : h('p', { class: 'muted' }, 'No runs yet - pick a deployment above, or: dagtrade run wick_reversal/AAPL')));
}


/* ----- live ----- */
const ago = (s) => s == null ? 'never' : s < 90 ? Math.round(s) + 's ago' : s < 5400 ? Math.round(s / 60) + 'm ago' : (s / 3600).toFixed(1) + 'h ago';
async function renderLive() {
  const draw = async () => {
    const L = await api('/api/live');
    const st = L.status || {}, ac = st.account, deps = st.deployments || [];
    const dryrun = (st.mode || '').includes('dry-run');
    const dot = $('#livedot'); dot.className = 'dot ' + (L.alive ? (L.paused ? 'warn' : 'on') : (L.enabled.length ? 'off' : ''));
    const item = (k, v) => h('div', { class: 'item' }, h('div', { class: 'k' }, k), h('div', { class: 'v' }, v));
    const runnerPill = L.alive ? h('span', { class: 'pill good' }, 'scanner running') : h('span', { class: 'pill bad' }, 'scanner NOT running');
    const banner = h('div', { class: 'card' }, h('div', { class: 'banner' },
      h('span', { class: 'pill paper' }, dryrun ? 'DRY RUN · no orders sent' : 'ALPACA PAPER'), runnerPill,
      L.paused ? h('span', { class: 'pill warn' }, 'PAUSED') : null,
      item('Heartbeat', ago(L.age)), item('Market', st.clock ? (st.clock.is_open ? 'open' : 'closed') : '–'),
      ac ? item('Paper equity', '$' + Math.round(ac.equity).toLocaleString()) : null, ac ? item('Buying power', '$' + Math.round(ac.buying_power).toLocaleString()) : null,
      h('span', { class: 'grow', style: 'flex:1' }),
      h('button', { onclick: async () => { await api('/api/live/' + (L.paused ? 'resume' : 'pause'), { method: 'POST' }); draw(); } }, L.paused ? 'Resume trading' : 'Pause trading'),
      h('a', { href: 'https://app.alpaca.markets/paper/dashboard/overview', target: '_blank', rel: 'noopener' }, h('button', {}, 'Open Alpaca ↗'))),
      !L.alive ? h('p', { class: 'note' }, L.scanner_error ? 'Scanner error: ' + L.scanner_error :
        L.enabled.length ? 'Start it with:  dagtrade serve   (dashboard + scanner)   or   dagtrade live run   (headless). Enabled strategies: ' + L.enabled.join(', ')
          : 'No strategy has [live] enabled = true yet. Add it to a strategy TOML (see README → Live paper trading).') : null,
      st.error ? h('p', { class: 'err note' }, st.error) : null);
    const cards = deps.map((d) => {
      const dc = d.decision || {}, o = d.orders || [];
      const pos = d.position || 0;
      const nodes = Object.entries(dc.nodes || {}).slice(0, 8).map(([k, v]) => `${k}=${v}`).join(' · ');
      return h('div', { class: 'card dep' }, h('h3', {}, d.label), h('div', { class: 'tag-s' }, `${d.base_tf} candles · order size $${Math.round((d.decision || {}).usd || 0).toLocaleString()}`),
        d.error ? h('p', { class: 'err' }, d.error) : null,
        h('dl', {},
          h('dt', {}, 'Wants'), h('dd', {}, dc.target == null ? '–' : (dc.target > 0 ? 'LONG' : dc.target < 0 ? 'SHORT' : 'FLAT') + (dc.entry ? '  · new order this candle' : '')),
          h('dt', {}, 'Position'), h('dd', {}, pos === 0 ? 'flat' : `${pos > 0 ? 'long' : 'short'} ${Math.abs(pos)} sh${d.locked ? '' : ''}`),
          h('dt', {}, 'Stop / target'), h('dd', {}, `${dc.stop != null ? dc.stop.toFixed(2) : '–'} / ${dc.take != null ? dc.take.toFixed(2) : '–'}`),
          h('dt', {}, 'Last bar'), h('dd', {}, `${d.last_bar || '–'}  @ ${dc.price != null ? dc.price.toFixed(2) : '–'}`),
          h('dt', {}, 'Working orders'), h('dd', {}, o.length ? o.map((x) => `${x.kind} ${x.side} ${x.qty} ${x.status}`).join(', ') : 'none'),
          h('dt', {}, 'Last action'), h('dd', {}, d.last_action ? `${d.last_action.text}  (${d.last_action.ts.slice(5, 16).replace('T', ' ')}Z)` : '–'),
          nodes ? h('dt', {}, 'Node values') : null, nodes ? h('dd', { class: 'tag-s' }, nodes) : null,
          d.last_error ? h('dt', { class: 'err' }, 'Error') : null, d.last_error ? h('dd', { class: 'err' }, d.last_error) : null),
        h('a', { href: '#/run/' + d.chart_run }, h('button', {}, 'Open live chart')));
    });
    const jr = h('div', { class: 'card' }, h('h2', {}, 'Journal'), h('div', { class: 'scroll', style: 'max-height:420px;overflow:auto' },
      h('table', { class: 'log' }, h('tbody', {}, L.journal.map((r) => h('tr', { class: r.type }, h('td', { class: 't' }, r.ts.slice(5, 19).replace('T', ' ')), h('td', {}, r.label), h('td', { class: 'ty' }, r.type), h('td', {}, r.text)))))),
      L.journal.length ? null : h('p', { class: 'muted' }, 'Nothing yet.'));
    app.replaceChildren(h('h1', {}, 'Live paper trading'),
      h('p', { class: 'sub' }, 'The scanner re-runs each strategy\'s DAG whenever a bar closes and sends the resulting orders to your Alpaca paper account. Use Alpaca\'s own dashboard for account P&L; this page shows what our code decided and why.'),
      banner, deps.length ? h('div', { class: 'deps' }, cards) : null, jr);
  };
  await draw();
  livePoll = setInterval(() => draw().catch(() => {}), 10000);
}

/* ----- run detail ----- */
const hidden = new Set();   // series ids the user toggled
async function renderRun(P) {
  const { meta, trades, chart } = P;
  const m = meta.metrics, intraday = !['1d', '1w'].includes(meta.base_tf);
  $('#crumb').textContent = `/ ${meta.strategy} · ${meta.ticker}`;
  if (STATIC) $('#brand').removeAttribute('href');
  const root = h('div', {});
  root.append(
    h('div', { style: 'display:flex;gap:12px;align-items:baseline;flex-wrap:wrap' }, h('h1', {}, `${meta.strategy} on ${meta.ticker}`),
      h('span', { class: 'badge' }, meta.base_tf + ' bars'), h('span', { class: 'tag-s' }, `${meta.start} → ${meta.end}`),
      meta.live ? h('span', { class: 'pill paper', title: 'Rebuilt from fresh bars each time a bar closes. Violet squares are real fills in the Alpaca paper account.' }, 'LIVE · ' + (meta.mode || 'paper')) : null,
      meta.causal && /OK/.test(meta.causal) ? h('span', { class: 'badge', title: meta.causal }, '✓ lookahead-checked') : null),
    h('p', { class: 'sub' }, meta.description || ''));
  if (meta.insights && meta.insights.length)
    root.append(h('div', { class: 'card notes' }, h('h2', {}, 'Read this first'), h('ul', {}, meta.insights.map((i) => h('li', {}, i)))));
  root.append(tiles(m, meta), scorecard(m, meta));
  root.append(equityCard(chart, meta), priceCard(chart, meta, intraday, trades));
  root.append(dagCard(meta), tradesCard(trades, chart));
  app.replaceChildren(root);
  current = { P };
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

function toPts(t, v) { const out = []; for (let i = 0; i < t.length; i++) if (v[i] != null) out.push({ time: t[i], value: v[i] }); return out; }

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
    h('p', { class: 'note', style: 'margin:-6px 0 8px' }, '▲▼ simulated entries (fill at the NEXT bar open) · ■ violet = real paper-account fills · ● exits (green = win, red = loss, with return and reason). Toggle DAG nodes below to stack their output under the chart.'),
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
    overlays.forEach(([k, s]) => {
      if (hidden.has(k)) return;
      const ls = main.addLineSeries({ color: colors[all.findIndex(([kk]) => kk === k) % colors.length], lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false, title: k });
      ls.setData(toPts(t, s.v));
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
function lowerBound(a, x) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; a[m] < x ? (lo = m + 1) : (hi = m); } return lo; }

/* ---- DAG diagram ---- */
function dagCard(meta) {
  const dag = meta.dag, nodes = dag.nodes;
  const W = 190, H = 52, GX = 70, GY = 18;
  const byDepth = {};
  nodes.forEach((n) => { (byDepth[n.depth] = byDepth[n.depth] || []).push(n); });
  const pos = {};
  let maxY = 0, maxX = 0;
  Object.entries(byDepth).forEach(([d, ns]) => ns.forEach((n, i) => { pos[n.id] = { x: 10 + d * (W + GX), y: 10 + i * (H + GY) }; maxY = Math.max(maxY, pos[n.id].y + H); maxX = Math.max(maxX, pos[n.id].x + W); }));
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg'); svg.setAttribute('width', maxX + 20); svg.setAttribute('height', maxY + 20);
  const el = (n, a, txt) => { const e = document.createElementNS(NS, n); for (const [k, v] of Object.entries(a || {})) e.setAttribute(k, v); if (txt != null) e.textContent = txt; return e; };
  nodes.forEach((n) => n.inputs.forEach((i) => { const a = pos[i], b = pos[n.id]; const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
    svg.append(el('path', { d: `M${x1},${y1} C${x1 + GX / 2},${y1} ${x2 - GX / 2},${y2} ${x2},${y2}` })); }));
  const plotted = new Set(Object.values(meta.node_info || {}).map((i) => i.node));
  nodes.forEach((n) => {
    const p = pos[n.id];
    const g = el('g', { class: 'node', transform: `translate(${p.x},${p.y})` });
    const ps = Object.entries(n.params || {}).filter(([k]) => !['has_stop', 'has_take', 'op', 'const', 'col'].includes(k)).map(([k, v]) => `${k}=${v}`).join(' ');
    g.append(el('rect', { class: 'box' + (plotted.has(n.id) ? ' on' : ''), width: W, height: H, rx: 8 }));
    g.append(el('text', { x: 10, y: 20 }, n.id.length > 26 ? n.id.slice(0, 25) + '…' : n.id));
    g.append(el('text', { class: 'sm', x: 10, y: 35 }, `${n.kind} · ${n.tf}`));
    if (ps) g.append(el('text', { class: 'sm', x: 10, y: 47 }, ps.length > 30 ? ps.slice(0, 29) + '…' : ps));
    g.append(el('title', {}, n.id + '\n' + ps));
    g.addEventListener('click', () => {
      const ks = Object.entries(meta.node_info || {}).filter(([, i]) => i.node === n.id).map(([k]) => k);
      if (!ks.length) return;
      const allHidden = ks.every((k) => hidden.has(k));
      ks.forEach((k) => (allHidden ? hidden.delete(k) : hidden.add(k)));
      const pc = document.querySelector('.chips').closest('.card');
      document.querySelectorAll('.chips .chip').forEach((c) => c.classList.toggle('on', !hidden.has(c.dataset.k)));
      pc.__draw(); pc.scrollIntoView({ behavior: 'smooth' });
    });
    svg.append(g);
  });
  return h('div', { class: 'card' }, h('h2', {}, `Strategy DAG · ${nodes.length} nodes`),
    h('p', { class: 'note', style: 'margin:-6px 0 8px' }, 'Data flows left → right. Each node says what kind it is and the timeframe it runs on; slower-timeframe values only reach faster nodes after their bar closes. Click a node to show/hide its output on the chart.'),
    h('div', { class: 'dag' }, svg));
}

/* ---- trades ---- */
function tradesCard(trades, chart) {
  const cols = [['entry_time', 'Entry', (r) => when(r.entry_time), 'l'], ['exit_time', 'Exit', (r) => when(r.exit_time), 'l'], ['side', 'Side', (r) => r.side, 'l'],
    ['entry_price', 'Entry px', (r) => num(r.entry_price)], ['exit_price', 'Exit px', (r) => num(r.exit_price)],
    ['ret', 'Return', (r) => h('span', { class: cls(r.pnl) }, pct(r.ret, 2))], ['pnl', 'P&L $', (r) => h('span', { class: cls(r.pnl) }, Math.round(r.pnl).toLocaleString())],
    ['bars_held', 'Bars', (r) => r.bars_held], ['exit_reason', 'Exit by', (r) => r.exit_reason, 'l'], ['mae', 'Worst dip', (r) => pct(r.mae, 1)], ['mfe', 'Best run-up', (r) => pct(r.mfe, 1)]];
  let key = 'entry_time', dir = 1;
  const wrap = h('div', { class: 'scroll', style: 'max-height:480px;overflow:auto' });
  const draw = () => {
    const rows = trades.slice().sort((a, b) => (a[key] > b[key] ? 1 : -1) * dir);
    wrap.replaceChildren(h('table', {}, h('thead', {}, h('tr', {}, cols.map(([k, t, , c]) => h('th', { class: 'sortable ' + (c || ''), onclick: () => { dir = key === k ? -dir : 1; key = k; draw(); } }, t + (key === k ? (dir > 0 ? ' ▲' : ' ▼') : ''))))),
      h('tbody', {}, rows.map((r) => h('tr', { class: 'click', onclick: () => priceCard.focus(r) }, cols.map(([, , f, c]) => h('td', { class: c || '' }, f(r))))))));
  };
  draw();
  return h('div', { class: 'card' }, h('h2', {}, `Trades (${trades.length})`), h('p', { class: 'note', style: 'margin:-6px 0 8px' }, 'Click a trade to jump the chart to it.'), trades.length ? wrap : h('p', { class: 'muted' }, 'No trades were taken.'));
}

render();
})();
