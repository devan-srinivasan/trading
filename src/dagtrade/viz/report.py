"""Standalone HTML report: one file, no server, no internet."""
from __future__ import annotations

import json
from pathlib import Path

from ..data.source import project_root
from ..store import RunStore
from .payload import run_payload

STATIC = Path(__file__).parent / "static"


def write_report(run_id: str, out: str | None = None) -> Path:
    store = RunStore()
    payload = run_payload(store, run_id)
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "style.css").read_text()
    html = html.replace('<link rel="stylesheet" href="/static/style.css">', f"<style>{css}</style>")
    scripts = ["lightweight-charts.js", "js/core.js", "js/run.js", "js/backtests.js", "js/live.js", "js/dags.js", "js/logs.js", "js/main.js"]
    blob = json.dumps(payload).replace("</", "<\\/")
    for name in scripts:
        tag = f'<script src="/static/{name}"></script>'
        js = (STATIC / name).read_text().replace("</script>", "<\\/script>")
        data = f"<script>window.STATIC_RUN = {blob};</script>" if name == "js/core.js" else ""
        html = html.replace(tag, f"{data}<script>{js}</script>")
    path = Path(out) if out else project_root() / "runs" / run_id / "report.html"
    path.write_text(html)
    return path
