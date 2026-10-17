/* Logs page: every area's log file, newest first, with level / search filters and auto-refresh. */
const logsView = { area: 'all', level: 'INFO', q: '', n: 300 };
const LOG_AREAS = [['all', 'Everything'], ['errors', 'Warnings & errors'], ['live', 'Live scanner'], ['activity', 'Live activity'],
  ['backtest', 'Backtests'], ['data', 'Market data'], ['server', 'Server']];

async function renderLogs() {
  const tabs = h('div', { class: 'seg' });
  const levelSel = h('select', { onchange: (e) => { logsView.level = e.target.value; draw(); } },
    ['INFO', 'WARNING', 'ERROR'].map((l) => h('option', { value: l }, l === 'INFO' ? 'All levels' : l + '+')));
  levelSel.value = logsView.level;
  let qTimer = null;
  const search = h('input', { class: 'inp', type: 'search', placeholder: 'Search messages…', value: logsView.q, style: 'flex:1 1 240px',
    oninput: (e) => { logsView.q = e.target.value; clearTimeout(qTimer); qTimer = setTimeout(draw, 250); } });
  const body = h('div', { class: 'scroll' });
  const count = h('span', { class: 'muted small' });
  const drawTabs = () => tabs.replaceChildren(...LOG_AREAS.map(([v, l]) =>
    h('button', { class: logsView.area === v ? 'on' : '', onclick: () => { logsView.area = v; drawTabs(); draw(); } }, l)));
  drawTabs();
  const lvlCls = { ERROR: 'bad', CRITICAL: 'bad', WARNING: 'warn', INFO: 'muted' };
  const draw = async () => {
    const rows = await api(`/api/logs?area=${logsView.area}&level=${logsView.level}&n=${logsView.n}&q=${encodeURIComponent(logsView.q)}`);
    live.updated = Date.now();
    count.textContent = `${rows.length} entries${rows.length >= logsView.n ? ' (newest ' + logsView.n + ')' : ''}`;
    body.replaceChildren(rows.length ? h('table', { class: 'tbl log logs' }, h('tbody', {}, rows.map((r) => h('tr', {},
      h('td', { class: 'l small muted nowrap' }, r.time),
      h('td', { class: 'l' }, h('span', { class: 'pill sm ' + (lvlCls[r.level] || 'info') }, r.level)),
      h('td', { class: 'l small muted nowrap' }, r.logger),
      h('td', { class: 'l msg' }, r.message)))))
      : h('p', { class: 'muted' }, 'Nothing here yet.'));
  };
  app.replaceChildren(h('div', {},
    h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Logs'),
      h('p', { class: 'sub', style: 'margin-bottom:0' }, 'Everything dagtrade does, by area. Files live in ', h('code', {}, 'logs/'), ' (rotated at 5 MB); ',
        h('code', {}, 'dagtrade logs'), ' shows the same in the terminal.')), refreshControls(draw)),
    h('div', { class: 'card' }, h('div', { class: 'filters' }, tabs, levelSel, search, count), body)));
  await draw();
  startPolling(draw);
}
