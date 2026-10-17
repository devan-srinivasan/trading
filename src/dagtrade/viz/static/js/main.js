/* Router: #/ DAGs · #/dag/<name> one DAG · #/backtests · #/run/<id> one backtest · #/live active DAGs · #/live/<id> one live DAG */
const app = $('#app');

async function render() {
  disposeCharts();
  clearInterval(live.timer);
  const hash = location.hash || '#/';
  $('#crumb').textContent = '';
  const route = hash.startsWith('#/live') ? 'live' : hash.startsWith('#/logs') ? 'logs'
    : (hash.startsWith('#/backtests') || hash.startsWith('#/run/')) ? 'backtests' : 'dags';
  document.querySelectorAll('nav .navlink').forEach((a) => a.classList.toggle('active', a.dataset.route === route));
  try {
    if (STATIC) return renderRun(STATIC);
    let m = hash.match(/^#\/run\/(.+)$/);
    if (m) return renderRun(await api('/api/run/' + encodeURIComponent(m[1])));
    m = hash.match(/^#\/live\/(.+)$/);
    if (m) return await renderLiveDag(decodeURIComponent(m[1]));
    if (hash === '#/live') return await renderLive();
    if (hash === '#/logs') return await renderLogs();
    m = hash.match(/^#\/dag\/(.+)$/);
    if (m) return await renderDag(decodeURIComponent(m[1]));
    if (hash === '#/backtests') return await renderHome();
    return await renderDags();
  } catch (e) {
    app.replaceChildren(h('div', { class: 'card err' }, 'Error: ' + e.message));
  }
}
window.addEventListener('hashchange', render);

render();
