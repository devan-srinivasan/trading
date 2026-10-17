/* Backtests: the run form (background job + progress bar) and the searchable list of results.
   Both are reusable: the Backtests page shows all strategies, a DAG page shows one. */
const filt = { q: '', strategy: '', ticker: '', tf: '' };
const listState = { sortKey: 'created', sortDir: -1, latestOnly: true };
const isoDay = (d) => d.toISOString().slice(0, 10);
const INTERVAL_OPTS = [['', 'strategy default'], ['1m', '1 minute'], ['5m', '5 minutes'], ['15m', '15 minutes'],
  ['30m', '30 minutes'], ['1h', '1 hour'], ['1d', '1 day']];
const field = (label, el) => h('label', { class: 'field' }, h('span', {}, label), el);

/* "Flat by close" (day trade) checkbox tied to an interval picker: ticked for intraday intervals, off for daily.
   With "strategy default" the ticker's TOML decides (value() = null). */
function flatByClose(ivSel) {
  const box = h('input', { type: 'checkbox' });
  const wrap = h('label', { class: 'toggle', style: 'height:36px' }, box, h('span', {}, 'flat by close'));
  const sync = () => {
    const v = ivSel.value, daily = ['1d', '1w'].includes(v);
    box.disabled = !v || daily; box.checked = !!v && !daily;
    wrap.title = !v ? "set by the ticker's TOML (e.g. SPY@5m.toml)" : daily ? 'needs intraday candles'
      : 'Day trade: no new entries in the last 30 minutes, out of the position before 16:00 ET';
  };
  ivSel.addEventListener('change', sync); sync();
  return { el: wrap, value: () => (ivSel.value ? box.checked : null) };
}

/* A strategy picker + ticker box with suggestions. `fixed` = a strategy name to lock it to. */
function strategyTicker(strats, fixed, idSuffix) {
  const stratSel = h('select', {}, strats.map((s) => h('option', { value: s.name }, s.name)));
  if (fixed) stratSel.value = fixed;
  const list = h('datalist', { id: 'tickers-' + idSuffix });
  const ticker = h('input', { class: 'inp', list: 'tickers-' + idSuffix, placeholder: 'e.g. AAPL', style: 'width:110px;text-transform:uppercase' });
  const desc = h('p', { class: 'note', style: 'margin:10px 0 0' });
  const sync = () => {
    const s = strats.find((x) => x.name === stratSel.value);
    desc.textContent = fixed ? '' : (s ? s.description : '');
    list.replaceChildren(...(s ? s.tickers : []).map((t) => h('option', { value: t })));
    if (!ticker.value && s && s.tickers.length) ticker.value = s.tickers[0];
  };
  stratSel.addEventListener('change', () => { ticker.value = ''; sync(); });
  sync();
  const fields = [fixed ? null : field('Strategy', stratSel), field('Ticker', h('span', {}, ticker, list))].filter(Boolean);
  return { stratSel, ticker, desc, fields };
}

/* Advanced run options shared by the backtest and go-live forms: start at a state, extra inputs (JSON). */
function advancedOptions(strats, stratSel) {
  const atSel = h('select', {});
  const inputsIn = h('input', { class: 'inp', placeholder: '{"risk": 0.01}  - every state sees these as ctx.inputs (plus "cash")', style: 'width:100%' });
  const sync = () => {
    const s = strats.find((x) => x.name === stratSel.value);
    const names = s && s.dag ? s.dag.nodes.map((n) => n.id) : [];
    atSel.replaceChildren(...names.map((n, i) => h('option', { value: i ? n : '' }, i ? n : `${n} (start)`)));
  };
  stratSel.addEventListener('change', sync); sync();
  const el = h('details', { class: 'advanced' }, h('summary', { class: 'small muted' }, 'Advanced: start state, inputs'),
    h('div', { class: 'form', style: 'margin-top:8px' }, field('Start at state', atSel), h('label', { class: 'field', style: 'flex:1 1 360px' }, h('span', {}, 'Inputs (JSON)'), inputsIn)),
    h('p', { class: 'note' }, 'Starting later than the start state only affects the first pass; after a trade (or a failure) it loops back to the start. A state that reads earlier outputs needs them in the inputs.'));
  return {
    el,
    value: () => {
      let inputs = {};
      if (inputsIn.value.trim()) inputs = JSON.parse(inputsIn.value);             // throws -> shown by the caller
      return { start_at: atSel.value || null, inputs };
    },
  };
}

function backtestForm(strats, fixed = null) {
  const st = strategyTicker(strats, fixed, 'bt');
  const today = new Date(), threeY = new Date(); threeY.setFullYear(today.getFullYear() - 3);
  const startIn = h('input', { class: 'inp', type: 'date', value: isoDay(threeY) });
  const endIn = h('input', { class: 'inp', type: 'date', value: isoDay(today) });
  const ivSel = h('select', {}, INTERVAL_OPTS.map(([v, l]) => h('option', { value: v }, l)));
  const flat = flatByClose(ivSel);
  const adv = advancedOptions(strats, st.stratSel);
  const fill = h('i', {});
  const progText = h('span', { class: 'note' }, '');
  let jobId = null;
  const stopBtn = h('button', { class: 'btn sm danger', hidden: true, onclick: async () => {
    if (!jobId) return;
    stopBtn.disabled = true; progText.textContent = 'stopping…';
    try { await api('/api/jobs/' + jobId + '/cancel', { method: 'POST' }); } catch (e) { progText.className = 'err'; progText.textContent = e.message; }
  } }, '■ Stop');
  const progBox = h('div', { class: 'progwrap', hidden: true }, h('div', { class: 'prog' }, fill), progText, stopBtn);
  const finish = () => { runBtn.disabled = false; stopBtn.hidden = true; jobId = null; };
  const runBtn = h('button', { class: 'btn primary', onclick: async () => {
    if (!st.ticker.value.trim()) { progBox.hidden = false; progText.className = 'err'; progText.textContent = 'Enter a ticker.'; return; }
    let extra;
    try { extra = adv.value(); } catch (err) { progBox.hidden = false; progText.className = 'err'; progText.textContent = 'Inputs are not valid JSON.'; return; }
    runBtn.disabled = true; progBox.hidden = false; progText.className = 'note'; fill.style.width = '2%';
    progText.textContent = 'starting…';
    try {
      const { id } = await api('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy: st.stratSel.value, ticker: st.ticker.value.trim().toUpperCase(), start: startIn.value,
          end: endIn.value, interval: ivSel.value, flat_by_close: flat.value(), ...extra }) });
      jobId = id; stopBtn.hidden = false; stopBtn.disabled = false;
      const poll = async () => {
        if (!document.body.contains(progBox)) return;                 // navigated away (the job keeps running)
        const j = await api('/api/jobs/' + id);
        fill.style.width = j.pct + '%';
        if (j.state === 'running') progText.textContent = `${Math.round(j.pct)}%  ·  ${j.stage}  ·  ${j.elapsed}s`;
        if (j.state === 'done') { location.hash = '#/run/' + j.run_id; return; }
        if (j.state === 'cancelled') { progText.className = 'note'; progText.textContent = `Stopped after ${j.elapsed}s. Nothing was saved.`; fill.style.width = '0%'; finish(); return; }
        if (j.state === 'error') { progText.className = 'err'; progText.textContent = 'Failed: ' + j.error; finish(); return; }
        setTimeout(poll, 400);
      };
      poll();
    } catch (e) { progText.className = 'err'; progText.textContent = e.message; finish(); }
  } }, 'Run backtest');
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, fixed ? 'Backtest this DAG' : 'Run a backtest'),
      h('span', { class: 'muted small' }, 'Historical candles · simulated fills')),
    h('div', { class: 'form' }, ...st.fields, field('Start', startIn), field('End', endIn), field('Candle interval', ivSel), field('Day trade', flat.el),
      h('div', { class: 'field' }, h('span', {}, ' '), runBtn)),
    st.desc, adv.el,
    h('p', { class: 'note' }, 'Interval = the candles the DAG runs on: any strategy works on any interval (a node that names its own interval keeps it). Flat by close = day trading, out before 16:00 ET. Minute candles over long periods need Alpaca keys.'),
    progBox);
}

/* The results table. `fixed` = only this strategy's runs. */
function backtestList(backtests, fixed = null) {
  let rowsAll = fixed ? backtests.filter((r) => r.strategy === fixed) : backtests.slice();
  const picked = new Set();                                         // selected run ids (for delete)
  let shown = [];                                                   // rows currently visible after filters
  const f = fixed ? { q: '', strategy: '', ticker: '', tf: '' } : filt;
  const uniq = (k) => [...new Set(rowsAll.map((r) => r[k]))].sort();
  const sel = (key, label) => {
    const s = h('select', { onchange: (e) => { f[key] = e.target.value; draw(); } },
      h('option', { value: '' }, label), uniq(key === 'tf' ? 'base_tf' : key).map((v) => h('option', { value: v }, v)));
    s.value = f[key];
    return s;
  };
  const search = h('input', { class: 'inp', type: 'search', placeholder: fixed ? 'Search ticker…' : 'Search strategy or ticker…', value: f.q,
    style: 'flex:1 1 220px;min-width:160px', oninput: (e) => { f.q = e.target.value.toLowerCase(); draw(); } });
  const day = (s) => (s || '').slice(0, 10);
  const cols = [
    ...(fixed ? [] : [['strategy', 'Strategy', (r) => h('a', { href: '#/dag/' + encodeURIComponent(r.strategy), onclick: (e) => e.stopPropagation() }, r.strategy), 'l']]),
    ['ticker', 'Ticker', (r) => r.ticker, 'l'], ['base_tf', 'Interval', (r) => r.base_tf],
    ['start', 'From', (r) => day(r.start)], ['end', 'To', (r) => day(r.end)],
    ['total_return', 'Return', (r) => h('span', { class: cls(r.total_return) }, pct(r.total_return))],
    ['bh_return', 'Buy&hold', (r) => pct(r.bh_return)],
    ['sharpe', 'Sharpe', (r) => num(r.sharpe)], ['max_drawdown', 'Max DD', (r) => pct(r.max_drawdown, 0)],
    ['n_trades', 'Trades', (r) => r.n_trades], ['win_rate', 'Win', (r) => pctp(r.win_rate, 0)],
    ['holdout_sharpe', 'Holdout Sharpe', (r) => num(r.holdout_sharpe)],
    ['luck_percentile', 'Beat random %', (r) => r.luck_percentile == null ? '–' : r.luck_percentile.toFixed(0)],
    ['created', 'Run at', (r) => r.created.slice(0, 16).replace('T', ' ')],
  ];
  const wrap = h('div', { class: 'scroll' });
  const count = h('span', { class: 'muted' });
  const selInfo = h('span', { class: 'muted small' });
  const delBtn = h('button', { class: 'btn sm danger', disabled: true, onclick: async () => {
    const ids = [...picked];
    if (!ids.length || !confirm(`Delete ${ids.length} backtest${ids.length === 1 ? '' : 's'}? Their saved results are removed from disk. This cannot be undone.`)) return;
    delBtn.disabled = true; selInfo.className = 'muted small'; selInfo.textContent = 'deleting…';
    try {
      const r = await api('/api/runs/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }) });
      const gone = new Set(ids);
      rowsAll = rowsAll.filter((x) => !gone.has(x.id));
      picked.clear(); draw();
      selInfo.textContent = `deleted ${r.deleted} · ${(r.freed / 1e6).toFixed(1)} MB freed`;
    } catch (e) { selInfo.className = 'err small'; selInfo.textContent = e.message; syncSel(); }
  } }, 'Delete selected');
  const allBox = h('input', { type: 'checkbox', title: 'Select all shown', onclick: (e) => e.stopPropagation(),
    onchange: (e) => { shown.forEach((r) => (e.target.checked ? picked.add(r.id) : picked.delete(r.id))); draw(); } });
  const syncSel = () => {
    const n = shown.filter((r) => picked.has(r.id)).length;
    allBox.checked = shown.length > 0 && n === shown.length;
    allBox.indeterminate = n > 0 && n < shown.length;
    delBtn.disabled = picked.size === 0;
    delBtn.textContent = picked.size ? `Delete ${picked.size} selected` : 'Delete selected';
  };
  const draw = () => {
    let rows = rowsAll.filter((r) => (!f.q || `${r.strategy} ${r.ticker}`.toLowerCase().includes(f.q))
      && (!f.strategy || r.strategy === f.strategy) && (!f.ticker || r.ticker === f.ticker) && (!f.tf || r.base_tf === f.tf));
    if (listState.latestOnly) { const seen = new Set(); rows = rows.filter((r) => { const k = `${r.strategy}/${r.ticker}/${r.base_tf}/${day(r.start)}/${day(r.end)}`; if (seen.has(k)) return false; seen.add(k); return true; }); }
    const { sortKey, sortDir } = listState;
    rows.sort((a, b) => ((a[sortKey] ?? -1e18) > (b[sortKey] ?? -1e18) ? 1 : -1) * sortDir);
    count.textContent = ` ${rows.length} of ${rowsAll.length}`;
    shown = rows;
    const live = new Set(rowsAll.map((r) => r.id));
    [...picked].forEach((id) => { if (!live.has(id)) picked.delete(id); });
    const rowBox = (r) => h('input', { type: 'checkbox', checked: picked.has(r.id), onclick: (e) => e.stopPropagation(),
      onchange: (e) => { e.target.checked ? picked.add(r.id) : picked.delete(r.id); e.target.closest('tr').classList.toggle('picked', e.target.checked); syncSel(); } });
    wrap.replaceChildren(rows.length ? h('table', {},
      h('thead', {}, h('tr', {}, h('th', { class: 'l pick' }, allBox), cols.map(([k, t, , c]) => h('th', { class: 'sortable ' + (c || ''), onclick: () => { listState.sortDir = listState.sortKey === k ? -listState.sortDir : -1; listState.sortKey = k; draw(); } }, t + (listState.sortKey === k ? (listState.sortDir > 0 ? ' ▲' : ' ▼') : ''))))),
      h('tbody', {}, rows.map((r) => h('tr', { class: 'click' + (picked.has(r.id) ? ' picked' : ''), onclick: () => (location.hash = '#/run/' + r.id) },
        h('td', { class: 'l pick', onclick: (e) => { e.stopPropagation(); if (e.target.tagName !== 'INPUT') e.currentTarget.firstChild.click(); } }, rowBox(r)),
        cols.map(([, , fn, c]) => h('td', { class: c || '' }, fn(r)))))))
      : h('p', { class: 'muted' }, rowsAll.length ? 'No backtests match.' : 'No backtests yet.'));
    syncSel();
  };
  draw();
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, fixed ? 'Backtest results' : 'Backtest results', count),
      h('label', { class: 'toggle' }, h('input', { type: 'checkbox', checked: listState.latestOnly, onchange: (e) => { listState.latestOnly = e.target.checked; draw(); } }), h('span', {}, 'hide repeat runs'))),
    h('div', { class: 'filters' }, search, fixed ? null : sel('strategy', 'All strategies'), sel('ticker', 'All tickers'), sel('tf', 'All intervals'),
      h('div', { class: 'sel-actions' }, selInfo, delBtn)),
    wrap);
}

async function renderHome() {
  const [runs, strats] = await Promise.all([api('/api/runs'), api('/api/strategies')]);
  app.replaceChildren(h('div', {},
    h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Backtests'),
      h('p', { class: 'sub', style: 'margin-bottom:0' }, 'Historical candles, simulated fills. Every result is saved here; live trading is separate (Live tab).'))),
    backtestForm(strats), backtestList(runs.filter((r) => !r.id.startsWith('live_')))));
}
