/* dagtrade dashboard: shared helpers, theme, chart plumbing (loaded first; plain scripts share one global scope) */
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
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) el.append(c.nodeType ? c : document.createTextNode(c));
  return el;
}
const money = (x, d = 2) => x == null ? '–' : (x < 0 ? '-' : '') + '$' + Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const signedMoney = (x) => x == null ? '–' : (x >= 0 ? '+' : '-') + '$' + Math.abs(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const ago = (s) => s == null ? 'never' : s < 90 ? Math.round(s) + 's ago' : s < 5400 ? Math.round(s / 60) + 'm ago' : (s / 3600).toFixed(1) + 'h ago';
const isoTime = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '–';
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
function disposeCharts(group) {                 // all charts, or only those tagged with `group`
  const keep = [];
  charts.forEach((c) => { if (!group || c.__group === group) { try { c.remove(); } catch (e) {} } else keep.push(c); });
  charts = keep;
}
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

/* A line with real gaps: points inside a gap (and the segment leading out of one) are drawn transparent. */
function gappedLine(t, v) {
  let last = v.find((x) => x != null);
  return t.map((tt, i) => {
    if (v[i] == null) return { time: tt, value: last ?? 0, color: 'transparent' };
    const startsRun = i > 0 && v[i - 1] == null;
    last = v[i];
    return startsRun ? { time: tt, value: v[i], color: 'transparent' } : { time: tt, value: v[i] };
  });
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

/* ---- trade boxes: a translucent rectangle per trade, entry corner -> exit corner (Lightweight Charts primitive) ---- */
function hexA(hex, a) {                       // "#1baf7a" + alpha -> rgba()
  const n = parseInt(hex.replace('#', ''), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
class TradeBoxes {
  constructor(trades) {
    const good = css('--good'), bad = css('--bad');
    this.items = trades.map((t) => ({ t1: t.entry_time, t2: t.exit_time, p1: t.entry_price, p2: t.exit_price,
      fill: hexA(t.pnl > 0 ? good : bad, 0.25), line: hexA(t.pnl > 0 ? good : bad, 0.85) }));
    this.boxes = [];
    const self = this;
    this._view = {
      update() {
        const ts = self.chart.timeScale(), s = self.series;
        self.boxes = self.items.map((b) => ({ ...b, x1: ts.timeToCoordinate(b.t1), x2: ts.timeToCoordinate(b.t2),
          y1: s.priceToCoordinate(b.p1), y2: s.priceToCoordinate(b.p2) }));
      },
      zOrder() { return 'bottom'; },
      renderer() {
        return { draw(target) {
          target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
            for (const b of self.boxes) {
              if (b.x1 == null || b.x2 == null || b.y1 == null || b.y2 == null) continue;
              const x = Math.round(Math.min(b.x1, b.x2) * hr), w = Math.max(Math.round(3 * hr), Math.round(Math.abs(b.x2 - b.x1) * hr));
              const y = Math.round(Math.min(b.y1, b.y2) * vr), hgt = Math.max(Math.round(3 * vr), Math.round(Math.abs(b.y2 - b.y1) * vr));
              ctx.fillStyle = b.fill; ctx.fillRect(x, y, w, hgt);
              ctx.strokeStyle = b.line; ctx.lineWidth = Math.max(1, hr); ctx.strokeRect(x + 0.5, y + 0.5, w - 1, hgt - 1);
            }
          });
        } };
      },
    };
  }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; requestUpdate(); }
  detached() {}
  updateAllViews() { this._view.update(); }
  paneViews() { return [this._view]; }
}

function lowerBound(a, x) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; a[m] < x ? (lo = m + 1) : (hi = m); } return lo; }
