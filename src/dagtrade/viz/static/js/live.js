/* Live paper trading: the table of active DAGs, and a page per live DAG. Auto-refreshes every 10 s. */
const live = { auto: true, timer: null, updated: 0 };
const STEP = {
  idle: { label: 'not reached yet', cls: 'muted' },
  active: { label: 'active: waiting for its condition', cls: 'warn' },
  done: { label: 'passed on this run', cls: 'good' },
};
const INTERVALS = [['', 'strategy default'], ['1m', '1 minute'], ['5m', '5 minutes'], ['15m', '15 minutes'],
  ['30m', '30 minutes'], ['1h', '1 hour'], ['1d', '1 day']];

function startPolling(fn) {
  clearInterval(live.timer);
  live.timer = setInterval(() => { if (live.auto && document.visibilityState === 'visible') fn().catch(() => {}); }, 10000);
}

/* Refresh button + auto-refresh toggle + "updated x ago" */
function refreshControls(onRefresh) {
  const stamp = h('span', { class: 'muted small' }, '');
  const tick = () => { stamp.textContent = live.updated ? 'updated ' + ago((Date.now() - live.updated) / 1000) : ''; };
  tick();
  const t = setInterval(() => { if (!document.body.contains(stamp)) clearInterval(t); else tick(); }, 1000);
  const btn = h('button', { class: 'btn', title: 'Refresh now', onclick: async () => { btn.disabled = true; try { await onRefresh(true); } finally { btn.disabled = false; } } }, '↻ Refresh');
  const auto = h('label', { class: 'toggle' }, h('input', { type: 'checkbox', checked: live.auto, onchange: (e) => { live.auto = e.target.checked; } }), h('span', {}, 'Auto (10 s)'));
  return h('div', { class: 'actions' }, stamp, auto, btn);
}

async function post(path, body) {
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
}

function stepDots(steps) {
  return h('span', { class: 'stepdots', title: steps.map((s) => `${s.name}: ${STEP[s.state].label}`).join('\n') },
    steps.map((s) => h('i', { class: 'dot ' + STEP[s.state].cls })));
}

/* The go-live form: strategy (unless `fixed`), ticker, interval, local or Alpaca paper. */
function goLiveForm(strats, fixed = null, onAdded = null) {
  const st = strategyTicker(strats, fixed, 'live');
  const ivSel = h('select', {}, INTERVALS.map(([v, l]) => h('option', { value: v }, l)));
  const flat = flatByClose(ivSel);
  const adv = advancedOptions(strats, st.stratSel);
  let mode = 'local';
  const seg = h('div', { class: 'seg' });
  const drawSeg = () => seg.replaceChildren(...[['local', 'Local paper'], ['alpaca', 'Alpaca paper']].map(([v, l]) =>
    h('button', { class: mode === v ? 'on' : '', onclick: () => { mode = v; drawSeg(); } }, l)));
  drawSeg();
  const msg = h('p', { class: 'small', style: 'margin:10px 0 0' });
  const goBtn = h('button', { class: 'btn primary', onclick: async () => {
    msg.className = 'small muted'; msg.textContent = '';
    try {
      let extra;
      try { extra = adv.value(); } catch (err) { throw new Error('Inputs are not valid JSON.'); }
      const e = await post('/api/live/dags', { strategy: st.stratSel.value, ticker: st.ticker.value.trim().toUpperCase(), interval: ivSel.value, flat_by_close: flat.value(), mode, ...extra });
      msg.className = 'small pos';
      msg.replaceChildren(`${e.id} is live (${e.mode} paper) and picks up the next closed candle. `, h('a', { href: '#/live/' + encodeURIComponent(e.id), style: 'text-decoration:underline' }, 'Open it →'));
      if (onAdded) await onAdded(e);
    } catch (err) { msg.className = 'small err'; msg.textContent = err.message; }
  } }, 'Go live');
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, fixed ? 'Run this DAG live' : 'Add a DAG'),
      h('span', { class: 'muted small' }, 'Live candles · Local paper: our engine, $10,000 per DAG · Alpaca paper: your Alpaca paper account')),
    h('div', { class: 'form' }, ...st.fields, field('Candle interval', ivSel), field('Day trade', flat.el), field('Trade with', seg), h('div', { class: 'field' }, h('span', {}, ' '), goBtn)),
    st.desc, adv.el, msg);
}

/* ======================================== the table ======================================== */
async function renderLive() {
  const strats = await api('/api/strategies');
  const head = h('div', {}), banner = h('div', {}), tableBox = h('div', { class: 'card' });

  const addCard = goLiveForm(strats, null, () => draw());

  const draw = async () => {
    const L = await api('/api/live');
    live.updated = Date.now();
    const sc = L.scanner;
    const dot = $('#livedot'); dot.className = 'dot ' + (sc.alive ? (L.paused ? 'warn' : 'good') : (L.dags.some((d) => d.status === 'running') ? 'bad' : 'muted'));
    head.replaceChildren(h('div', { class: 'page-head' },
      h('div', {}, h('h1', {}, 'Live paper trading'),
        h('div', { class: 'meta-row' },
          h('span', { class: 'pill ' + (sc.alive ? 'good' : 'bad') }, sc.alive ? '● scanner running' : '● scanner stopped'),
          L.clock ? h('span', { class: 'pill ' + (L.clock.is_open ? 'good' : 'muted') }, L.clock.is_open ? 'market open' : 'market closed') : null,
          L.paused ? h('span', { class: 'pill warn' }, 'orders paused') : null,
          sc.alive ? h('span', { class: 'muted small' }, 'heartbeat ' + ago(sc.age)) : null)),
      h('div', { class: 'actions' },
        !sc.alive ? h('button', { class: 'btn primary', onclick: async () => { try { await post('/api/live/scanner/start'); setTimeout(draw, 1500); } catch (e) { alert(e.message); } } }, '▶ Start scanner') : null,
        h('button', { class: 'btn', onclick: async () => { await post('/api/live/' + (L.paused ? 'resume' : 'pause')); draw(); } }, L.paused ? 'Resume orders' : 'Pause all orders'),
        L.dags.some((d) => d.mode === 'alpaca') ? h('a', { class: 'btn', href: 'https://app.alpaca.markets/paper/dashboard/overview', target: '_blank', rel: 'noopener' }, 'Alpaca dashboard ↗') : null,
        refreshControls(draw))));
    banner.replaceChildren(...[
      !sc.alive && L.dags.some((d) => d.status === 'running') ? h('div', { class: 'card notes' }, 'The scanner is not running, so live DAGs are not being checked. Press ', h('b', {}, 'Start scanner'), ', or run ', h('code', {}, 'dagtrade serve'), '.') : null,
      sc.error ? h('div', { class: 'card notes err' }, 'Scanner error: ' + sc.error) : null,
    ].filter(Boolean));

    const rows = L.dags;
    const actionBtns = (d) => h('div', { class: 'actions nowrap', onclick: (e) => e.stopPropagation() },
      d.status === 'running'
        ? h('button', { class: 'btn sm', onclick: async () => { await post('/api/live/dag/action', { id: d.id, action: 'stop' }); draw(); } }, '■ Stop')
        : h('button', { class: 'btn sm', onclick: async () => { try { await post('/api/live/dag/action', { id: d.id, action: 'start' }); draw(); } catch (e) { alert(e.message); } } }, '▶ Start'),
      h('a', { class: 'btn sm', href: '#/live/' + encodeURIComponent(d.id) }, 'Open'));
    const pnl = (d) => { const a = d.account; if (!a) return '–'; const v = (a.realized || 0) + (a.unrealized || 0); return h('span', { class: cls(v) }, signedMoney(v)); };
    tableBox.replaceChildren(
      h('div', { class: 'card-head' }, h('h2', {}, `Active DAGs `, h('span', { class: 'muted' }, `${rows.filter((d) => d.status === 'running').length} running · ${rows.length} total`))),
      rows.length ? h('div', { class: 'scroll' }, h('table', { class: 'tbl' },
        h('thead', {}, h('tr', {}, ['DAG', 'Interval', 'Orders to', 'Status', 'Pipeline', 'Position', 'Equity', 'P&L', 'Last candle', 'Last action', ''].map((t, i) => h('th', { class: i <= 4 || i >= 8 ? 'l' : '' }, t)))),
        h('tbody', {}, rows.map((d) => h('tr', { class: 'click', onclick: () => (location.hash = '#/live/' + encodeURIComponent(d.id)) },
          h('td', { class: 'l' }, h('b', {}, d.strategy), h('span', { class: 'muted' }, ' · ' + d.ticker), d.error ? h('div', { class: 'err small' }, d.error) : null),
          h('td', { class: 'l' }, (d.interval || 'default') + (d.flat_by_close ? ' · flat by close' : '')),
          h('td', { class: 'l' }, alpacaToggle(d, draw)),
          h('td', { class: 'l' }, h('span', { class: 'pill ' + (d.status === 'running' ? 'good' : 'muted') }, d.status)),
          h('td', { class: 'l' }, d.steps && d.steps.length ? h('span', {}, stepDots(d.steps), ' ', h('span', { class: 'small muted' }, d.current_step || '')) : h('span', { class: 'muted small' }, 'waiting for first candle')),
          h('td', {}, d.position ? (d.position > 0 ? 'long ' : 'short ') + Math.abs(d.position) : 'flat'),
          h('td', {}, d.account && d.account.equity != null ? money(d.account.equity, 0) : '–'),
          h('td', {}, pnl(d)),
          h('td', { class: 'l small' }, d.last_bar ? isoTime(d.last_bar) : '–'),
          h('td', { class: 'l small' }, d.last_action ? d.last_action.text : '–'),
          h('td', { class: 'l' }, actionBtns(d))))))) : h('p', { class: 'muted' }, 'Nothing live yet. Add a DAG above: it starts paper trading on the next closed candle.'));
  };
  app.replaceChildren(h('div', {}, head, banner, addCard, tableBox));
  await draw();
  startPolling(draw);
}

/* ======================================== one live DAG ======================================== */
const WINDOWS_UI = [['today', 'Today'], ['1w', '1 week'], ['1m', '1 month'], ['all', 'Since start']];
live.window = 'all';

/* The ONE place a mode switch lives: where this DAG's orders go. Same DAG, same fills ledger either way. */
function alpacaToggle(e, after) {
  const on = e.mode === 'alpaca';
  return h('label', { class: 'switch', title: on ? 'Orders go to your Alpaca paper account' : 'Orders are filled by the local paper engine',
    onclick: (ev) => ev.stopPropagation() },
    h('input', { type: 'checkbox', checked: on, onchange: async (ev) => {
      const want = ev.target.checked ? 'alpaca' : 'local';
      const msg = want === 'alpaca'
        ? `Send ${e.id}'s orders to your Alpaca paper account?\n\nThe local engine stops for this DAG (any local position is closed at the last price).`
        : `Fill ${e.id}'s orders locally again?\n\nPositions and orders already at Alpaca are NOT touched.`;
      if (!confirm(msg)) { ev.target.checked = !ev.target.checked; return; }
      try { await post('/api/live/dag/action', { id: e.id, action: 'mode', mode: want }); if (after) await after(true); }
      catch (err) { alert(err.message); ev.target.checked = !ev.target.checked; }
    } }), h('span', { class: 'track' }, h('i', {})), h('span', { class: 'small' }, 'Send orders to Alpaca'));
}

/* Restart the state machine at any state (built once, so the 10 s refresh never wipes what you type). */
function restartControl(D, redraw) {
  const e = D.entry, names = ((D.detail || {}).stages || []).map((s) => s.name);
  const atSel = h('select', {}, names.map((n) => h('option', { value: n }, n)));
  const memIn = h('input', { class: 'inp', placeholder: 'optional: outputs of earlier states as JSON, e.g. {"opening_range": {"high": 712.4, "low": 709.1}}', style: 'flex:1 1 320px;min-width:220px' });
  const msg = h('span', { class: 'small muted' });
  const btn = h('button', { class: 'btn sm', onclick: async () => {
    let memory = {};
    if (memIn.value.trim()) { try { memory = JSON.parse(memIn.value); } catch (err) { msg.className = 'small err'; msg.textContent = 'not valid JSON'; return; } }
    if (!confirm(`Restart ${e.id} at "${atSel.value}"? Its current progress is dropped (an open position keeps its stop-loss / take-profit at the broker).`)) return;
    try { await post('/api/live/dag/action', { id: e.id, action: 'restart', at: atSel.value, memory }); msg.className = 'small pos'; msg.textContent = "restarts on the scanner's next cycle"; await redraw(true); }
    catch (err) { msg.className = 'small err'; msg.textContent = err.message; }
  } }, '↻ Restart at');
  return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, 'Restart the state machine'),
    h('span', { class: 'muted small' }, 'start from any state; a state that reads earlier outputs needs them as JSON')),
    names.length ? h('div', { class: 'filters', style: 'margin:0' }, btn, atSel, memIn, msg) : h('p', { class: 'muted' }, 'Available once the DAG is running.'));
}

async function renderLiveDag(id) {
  $('#crumb').textContent = id;
  const top = h('div', {}), ctl = h('div', {}), results = h('div', {});
  app.replaceChildren(h('div', {}, top, ctl, results));
  let stamp;

  const draw = async (force) => {
    const D = await api(`/api/live/dag?id=${encodeURIComponent(id)}&window=${live.window}`);
    live.updated = Date.now();
    top.replaceChildren(...liveTop(D, draw));
    if (!ctl.firstChild || !ctl.firstChild.querySelector('option')) ctl.replaceChildren(restartControl(D, draw));
    const key = `${D.chart_updated}|${live.window}|${D.entry.mode}`;
    if (force !== true && key === stamp) return;
    stamp = key;
    disposeCharts();
    results.replaceChildren(...liveResults(D, draw));
  };
  await draw(true);
  startPolling(draw);
}

/* Results = performance (from the account's fills, over the chosen window) + live chart + trades + orders */
function liveResults(D, redraw) {
  const e = D.entry, R = D.report || {}, m = R.metrics;
  const meta = { ticker: e.ticker, benchmark: 'SPY' };
  const seg = h('div', { class: 'seg' }, WINDOWS_UI.map(([v, l]) =>
    h('button', { class: live.window === v ? 'on' : '', onclick: () => { live.window = v; redraw(true); } }, l)));
  const out = [h('div', { class: 'section-title' }, h('h2', {}, 'Performance'),
    h('span', { class: 'muted small' }, 'Computed only from this account\'s fills, valued on live candles: the same ledger and metrics as backtests.'),
    h('span', { class: 'grow' }), seg)];
  if (R.error) out.push(h('div', { class: 'card err' }, 'Could not compute metrics: ' + R.error));
  else if (!m || !m.strategy || !Object.keys(m.strategy).length) {
    out.push(h('div', { class: 'card muted' }, 'Not enough live history in this window yet (metrics need a few days of candles). ',
      m && m.trades ? `${m.trades.n_trades} closed trades so far.` : ''));
  } else {
    out.push(tiles(m, meta), scorecardLive(m, meta), equityCard({ equity: R.equity }, meta));
  }
  const intraday = !['1d', '1w'].includes((D.detail || {}).base_tf || '1d');
  out.push(h('div', { class: 'section-title' }, h('h2', {}, 'Live chart'),
    h('span', { class: 'muted small' }, 'Live candles, what each node output on them, and this account\'s real trades (an open trade runs to the latest candle).')));
  if (D.chart) out.push(priceCard(D.chart, null, intraday, D.trades), tradesCard(D.trades, D.chart, 'Trades'));
  else out.push(h('div', { class: 'card muted' }, 'The chart appears after the first candle closes while this DAG is running.'));
  out.push(ordersCard(D.detail.orders || [], e.mode === 'alpaca' ? 'sent to Alpaca' : 'local paper engine'));
  return out;
}

function scorecardLive(m, meta) {
  const card = scorecard(m, meta);                     // same scorecard; the holdout card is a backtest-only check
  const first = card.firstChild;
  return first || card;
}

function activityCard(D) {
  const j = D.journal || [];
  return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, 'Activity'), h('a', { class: 'btn sm', href: '#/logs' }, 'All logs →')),
    j.length ? h('div', { class: 'scroll', style: 'max-height:330px;overflow:auto' }, h('table', { class: 'tbl log' }, h('tbody', {}, j.map((r) =>
      h('tr', { class: r.type }, h('td', { class: 'l small muted nowrap' }, isoTime(r.ts)), h('td', { class: 'l' }, h('span', { class: 'pill sm ' + ({ error: 'bad', action: 'paper', fill: 'good', decision: 'muted', transition: 'warn', intent: 'paper' }[r.type] || 'info') }, r.type)), h('td', { class: 'l small' }, r.text))))))
      : h('p', { class: 'muted' }, 'Nothing yet.'));
}

function liveTop(D, redraw) {
  const e = D.entry, det = D.detail || {}, acct = det.account || {}, fsa = det.fsa || {};
  const stages = det.stages || [];
  const running = e.status === 'running';
  const act = async (body, confirmText) => {
    if (confirmText && !confirm(confirmText)) return;
    try { await post('/api/live/dag/action', { id: e.id, ...body }); await redraw(true); } catch (err) { alert(err.message); }
  };
  const head = h('div', { class: 'page-head' },
    h('div', {}, h('h1', {}, h('a', { href: '#/dag/' + encodeURIComponent(e.strategy), title: 'Open the DAG (backtest it, see its nodes)' }, e.strategy), ` · ${e.ticker}`),
      h('div', { class: 'meta-row' },
        h('span', { class: 'pill' }, (det.base_tf || e.interval || 'default') + ' candles'),
        h('span', { class: 'pill ' + (e.mode === 'alpaca' ? 'paper' : 'info') }, e.mode === 'alpaca' ? 'Alpaca paper' : 'Local paper'),
        h('span', { class: 'pill ' + (running ? 'good' : 'muted') }, running ? '● running' : 'stopped'),
        det.last_avail ? h('span', { class: 'muted small' }, 'last candle closed ' + isoTime(new Date(det.last_avail * 1000).toISOString())) : null)),
    h('div', { class: 'actions' },
      running ? h('button', { class: 'btn', onclick: () => act({ action: 'stop' }, e.mode === 'local' ? 'Stop this DAG? Any local position is closed at the last price.' : 'Stop this DAG? Orders already at Alpaca (take-profit / stop-loss) stay there.') }, '■ Stop')
        : h('button', { class: 'btn', onclick: () => act({ action: 'start' }) }, '▶ Start'),
      alpacaToggle(e, redraw),
      h('button', { class: 'btn danger', onclick: async () => { if (!confirm(`Remove ${e.id} from live trading?`)) return; await post('/api/live/dag/action', { id: e.id, action: 'remove' }); location.hash = '#/live'; } }, 'Remove'),
      refreshControls(redraw)));

  const pos = det.position || 0;
  const tile = (k, v, d, c) => h('div', { class: 'tile' }, h('div', { class: 'k' }, k), h('div', { class: 'v ' + (c || '') }, v), h('div', { class: 'd' }, d || ' '));
  const unreal = acct.unrealized || 0, real = acct.realized || 0;
  const tiles = h('div', { class: 'tiles' },
    e.mode === 'local' ? tile('Account equity', money(acct.equity, 2), `started with ${money(acct.start_cash, 0)} · cash ${money(acct.cash, 0)}`)
      : tile('Account', 'Alpaca paper', h('a', { href: 'https://app.alpaca.markets/paper/dashboard/overview', target: '_blank', rel: 'noopener' }, 'open the Alpaca dashboard ↗')),
    tile('Position', pos ? `${pos > 0 ? 'long' : 'short'} ${Math.abs(pos)}` : 'flat', acct.avg_price ? `avg ${money(acct.avg_price)} · last ${money(acct.last_price)}` : (acct.last_price ? `last ${money(acct.last_price)}` : '')),
    tile('Realized P&L', signedMoney(real), `${acct.closed_trades || 0} closed trades · ${acct.wins || 0} wins`, cls(real)),
    tile('Unrealized P&L', signedMoney(unreal), pos ? 'on the open position' : 'no open position', cls(unreal)),
    e.mode === 'local' ? tile('Return', pct(acct.return, 2), `fees ${money(acct.fees)}`, cls(acct.return)) : null,
    tile('Active state', fsa.current || '–', fsa.current ? `waited ${fsa.waited || 0} candle${fsa.waited === 1 ? '' : 's'}` + (fsa.max_wait ? ` of ${fsa.max_wait}` : '') : ''));

  /* ---- the state machine: where it is now, what each state found, and a restart control ---- */
  const dagDesc = det.dag ? { ...det.dag, _run: true } : null;
  const doneIds = stages.filter((s) => s.state === 'done').map((s) => s.name);
  const list = h('ol', { class: 'pipe' }, stages.map((s) => {
    const out = s.output && typeof s.output === 'object' ? Object.entries(s.output) : [];
    return h('li', { class: 'step ' + STEP[s.state].cls + (s.state === 'active' ? ' current' : '') },
      h('span', { class: 'dot ' + STEP[s.state].cls }),
      h('div', { class: 'step-body' },
        h('div', { class: 'step-title' }, h('b', {}, s.name), h('span', { class: 'muted small' }, ` ${s.kind} · ${s.interval}`),
          s.state === 'active' ? h('span', { class: 'pill warn sm' }, '← here') : null),
        h('div', { class: 'small ' + (s.state === 'done' ? 'pos' : 'muted') }, STEP[s.state].label
          + (s.state === 'active' ? ` · ${s.waited || 0} candle${s.waited === 1 ? '' : 's'}${s.max_wait ? ' of ' + s.max_wait : ''}${s.since ? ' since ' + isoTime(new Date(s.since * 1000).toISOString()) : ''}` : '')),
        out.length ? h('div', { class: 'kv' }, out.slice(0, 10).map(([k, v]) => h('span', {}, `${k}: ${v === true ? 'true' : v === false ? 'false' : typeof v === 'number' ? +v.toFixed(4) : v}`))) : null));
  }));
  const pipeCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, 'State machine'), h('span', { class: 'legend-dots small muted' },
      h('i', { class: 'dot warn' }), 'active', h('i', { class: 'dot good' }), 'passed', h('i', { class: 'dot muted' }), 'not reached')),
    dagDesc ? fsaDiagram(dagDesc, { active: fsa.current, done: doneIds }) : null,
    stages.length ? list : h('p', { class: 'muted' }, running ? 'Waiting for the first candle to close…' : 'Not running.'),
    h('p', { class: 'note' }, 'Every closed candle the active state looks at its candles: not yet → it waits (up to its max wait), success → the next state, failure / waited too long → its fail edge (default: the start). After the Trade closes its position it starts again.'));

  const out = [head];
  if (det.last_error) out.push(h('div', { class: 'card notes err' }, 'Last error: ' + det.last_error));
  out.push(tiles, h('div', { class: 'grid2' }, pipeCard, activityCard(D)));

  return out;
}
