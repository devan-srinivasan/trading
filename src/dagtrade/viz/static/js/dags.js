/* DAGs: the strategies themselves, independent of where they run. From a DAG you can backtest it (historical
   candles, simulated fills) or run it live (live candles, local or Alpaca paper) - the two are kept separate. */

function chainText(dag) {
  return dag ? dag.nodes.map((n) => n.id).join('  →  ') : '';
}

/* A DAG whose nodes name no interval runs on whatever candles you pick (daily, hourly, 5-minute...). */
const anyInterval = (dag) => dag && dag.nodes.every((n) => n.kind === 'trade' || n.agnostic);
/* "ok → gap_break · fail → opening_range" for one state (defaults in grey) */
const edgeText = (dag, id) => (dag.edges || []).filter((e) => e.from === id && !(e.default && e.to === id))
  .map((e) => h('span', { class: e.default ? 'muted' : '' }, `${e.outcome} → ${e.to}`));
const intervalPill = (s) => !s.dag ? null : h('span', { class: 'pill' },
  anyInterval(s.dag) ? 'any interval' : s.dag.base_tf + ' candles');
const setupsText = (s) => s.tickers.length ? 'tickers ' + s.tickers.join(' ')
  + (s.intervals && s.intervals.length ? ' · ready-made ' + s.intervals.join(', ') + ' setups' : '') : null;

async function renderDags() {
  const [strats, runs, L] = await Promise.all([api('/api/strategies'), api('/api/runs'), api('/api/live')]);
  const bt = runs.filter((r) => !r.id.startsWith('live_'));
  const cards = strats.map((s) => {
    const nBt = bt.filter((r) => r.strategy === s.name).length;
    const lv = L.dags.filter((d) => d.strategy === s.name);
    const running = lv.filter((d) => d.status === 'running').length;
    return h('div', { class: 'card dagcard click', onclick: () => (location.hash = '#/dag/' + encodeURIComponent(s.name)) },
      h('div', { class: 'card-head' }, h('h2', {}, s.name),
        h('div', { class: 'actions' }, intervalPill(s),
          running ? h('span', { class: 'pill good' }, `● ${running} live`) : null)),
      h('p', { class: 'small', style: 'margin:0 0 10px;color:var(--text-2)' }, s.description || ''),
      s.dag ? h('div', { class: 'chain' }, s.dag.nodes.map((n, i) => [i ? h('span', { class: 'arrow' }, '→') : null, h('span', { class: 'node-chip ' + n.kind }, n.id)]),
        h('span', { class: 'arrow', title: 'loops back to the start' }, '↺')) : h('p', { class: 'err small' }, s.error || ''),
      h('div', { class: 'meta-row small muted', style: 'margin-top:12px' },
        h('span', {}, `${nBt} backtest${nBt === 1 ? '' : 's'}`), h('span', {}, '·'),
        h('span', {}, `${lv.length} live instance${lv.length === 1 ? '' : 's'}`)),
      s.tickers.length ? h('div', { class: 'small muted', style: 'margin-top:4px' }, setupsText(s)) : null);
  });
  app.replaceChildren(h('div', {},
    h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'DAGs'),
      h('p', { class: 'sub', style: 'margin-bottom:0' }, 'Each strategy is a state machine: states wait for their condition and move on, ending in a Trade, then loop back. Open one to backtest it on historical candles, or to run it live on live candles - the two never mix.'))),
    h('div', { class: 'dag-grid' }, cards)));
}

async function renderDag(name) {
  $('#crumb').textContent = name;
  const [strats, runs, L] = await Promise.all([api('/api/strategies'), api('/api/runs'), api('/api/live')]);
  const s = strats.find((x) => x.name === name);
  if (!s) throw new Error(`no DAG called ${name}`);
  const bt = runs.filter((r) => !r.id.startsWith('live_'));
  const liveBox = h('div', {});
  const drawLive = async () => {
    const LL = await api('/api/live');
    const lv = LL.dags.filter((d) => d.strategy === name);
    liveBox.replaceChildren(h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', {}, 'Live instances ', h('span', { class: 'muted' }, String(lv.length))), h('a', { class: 'btn sm', href: '#/live' }, 'All live DAGs →')),
      lv.length ? h('div', { class: 'scroll' }, h('table', { class: 'tbl' },
        h('thead', {}, h('tr', {}, ['Ticker', 'Interval', 'Trades with', 'Status', 'Pipeline', 'Position', 'P&L', ''].map((t, i) => h('th', { class: i <= 4 ? 'l' : '' }, t)))),
        h('tbody', {}, lv.map((d) => {
          const a = d.account, v = a ? (a.realized || 0) + (a.unrealized || 0) : null;
          return h('tr', { class: 'click', onclick: () => (location.hash = '#/live/' + encodeURIComponent(d.id)) },
            h('td', { class: 'l' }, h('b', {}, d.ticker)), h('td', { class: 'l' }, (d.interval || 'default') + (d.flat_by_close ? ' · flat by close' : '')),
            h('td', { class: 'l' }, h('span', { class: 'pill ' + (d.mode === 'alpaca' ? 'paper' : 'info') }, d.mode === 'alpaca' ? 'Alpaca paper' : 'Local paper')),
            h('td', { class: 'l' }, h('span', { class: 'pill ' + (d.status === 'running' ? 'good' : 'muted') }, d.status)),
            h('td', { class: 'l' }, d.steps && d.steps.length ? stepDots(d.steps) : h('span', { class: 'muted small' }, '–')),
            h('td', {}, d.position ? (d.position > 0 ? 'long ' : 'short ') + Math.abs(d.position) : 'flat'),
            h('td', {}, v == null ? '–' : h('span', { class: cls(v) }, signedMoney(v))),
            h('td', {}, h('a', { class: 'btn sm', href: '#/live/' + encodeURIComponent(d.id) }, 'Open')));
        })))) : h('p', { class: 'muted' }, 'Not running live anywhere yet.')));
  };
  const params = Object.entries(s.defaults || {});
  const nodesCard = h('div', { class: 'card' },
    h('div', { class: 'card-head' }, h('h2', {}, 'States'), h('span', { class: 'muted small' }, s.dag ? `${s.dag.nodes.length} states · ` + (anyInterval(s.dag) ? 'runs on the interval you pick' : `executes on ${s.dag.base_tf} candles`) : '')),
    s.dag ? h('ol', { class: 'pipe' }, s.dag.nodes.map((n) => h('li', { class: 'step' }, h('span', { class: 'dot info' }),
      h('div', { class: 'step-body' }, h('div', { class: 'step-title' }, h('b', {}, n.id), h('span', { class: 'muted small' }, ` ${n.kind} · ${n.agnostic ? 'any interval' : n.tf}`)),
        h('div', { class: 'small muted' }, (n.history ? 'reads ' + n.history : '') + (n.max_wait ? ' · waits at most ' + n.max_wait : (n.kind === 'trade' ? '' : ' · waits as long as it takes'))),
        h('div', { class: 'kv edges' }, edgeText(s.dag, n.id)),
        Object.keys(n.params || {}).length ? h('div', { class: 'kv' }, Object.entries(n.params).filter(([, v]) => v !== null).map(([k, v]) => h('span', {}, `${k}: ${v}`))) : null))))
      : h('p', { class: 'err' }, s.error || 'cannot load'),
    params.length ? [h('h2', { style: 'margin:18px 0 8px' }, 'Default parameters'), h('div', { class: 'kv' }, params.map(([k, v]) => h('span', {}, `${k}: ${v}`))),
      h('p', { class: 'note' }, 'Override per ticker under [params] in strategies/' + name + '/<TICKER>.toml, or per ticker and interval in <TICKER>@5m.toml.')] : null);
  app.replaceChildren(h('div', {},
    h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, name),
      h('div', { class: 'meta-row' }, intervalPill(s),
        h('span', { class: 'muted small' }, 'strategies/' + name + '/strategy.py'))),
      h('div', { class: 'actions' }, h('a', { class: 'btn', href: '#/' }, '← All DAGs'))),
    h('p', { class: 'sub' }, s.description || ''),
    s.dag ? h('div', { class: 'card' }, h('h2', {}, 'State machine'), dagCard({ dag: s.dag, node_info: {} }).lastChild) : null,
    nodesCard,
    h('div', { class: 'section-title' }, h('h2', {}, 'Backtest'), h('span', { class: 'muted small' }, 'historical candles · simulated fills')),
    backtestForm(strats, name), backtestList(bt, name),
    h('div', { class: 'section-title' }, h('h2', {}, 'Live'), h('span', { class: 'muted small' }, 'live candles · real paper account')),
    goLiveForm(strats, name, drawLive), liveBox));
  await drawLive();
}
