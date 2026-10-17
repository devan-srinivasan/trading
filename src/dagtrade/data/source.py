"""DataSource: ask for bars of any ticker/timeframe/date-range; missing data is fetched and cached automatically."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .. import tf as tfm
from .providers import Provider, default_providers
from .resample import OHLCV, add_avail, resample

log = logging.getLogger("dagtrade.data")


def project_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "pyproject.toml").exists():
            return p
    return Path.cwd()


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _chunks(a: pd.Timestamp, b: pd.Timestamp, tf: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Split a download into pieces (a month of intraday candles, 10 years of daily) so it can report progress
    and be cancelled between requests."""
    step = pd.Timedelta(days=30) if tfm.is_intraday(tf) else pd.Timedelta(days=3650)
    out = []
    while a < b:
        out.append((a, min(a + step, b)))
        a = out[-1][1]
    return out


class BarStore:
    """Parquet cache: data/bars/<TICKER>/<tf>.parquet + <tf>.json (covered range, since holidays leave gaps)."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else project_root() / "data" / "bars"

    def _paths(self, ticker, tf):
        d = self.root / ticker.upper()
        return d / f"{tf}.parquet", d / f"{tf}.json"

    def load(self, ticker, tf) -> tuple[pd.DataFrame, tuple[pd.Timestamp, pd.Timestamp] | None]:
        pq, js = self._paths(ticker, tf)
        if not pq.exists():
            return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC")), None
        meta = json.loads(js.read_text())
        return pd.read_parquet(pq), (_ts(meta["start"]), _ts(meta["end"]))

    def save(self, ticker, tf, df, covered):
        pq, js = self._paths(ticker, tf)
        pq.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(pq)
        js.write_text(json.dumps({"start": str(covered[0]), "end": str(covered[1])}))


class DataSource:
    def __init__(self, providers: list[Provider] | None = None, store: BarStore | None = None,
                 fetch_missing: bool = True, slack: pd.Timedelta | None = None):
        self.slack = slack            # how stale the cache tail may be before refetching (live: seconds)
        self.providers = providers if providers is not None else default_providers()
        self.store = store or BarStore()
        self.fetch_missing = fetch_missing
        # optional callback(label, done, total) between download chunks; it may raise to abort (cancel a backtest).
        # An aborted download caches nothing, so the cache never holds half a range.
        self.on_chunk = None

    # -- fetching ---------------------------------------------------------------------------
    def _fetch(self, ticker, tf, start, end) -> pd.DataFrame:
        last_err = None
        for p in self.providers:
            try:
                df = p.fetch(ticker, tf, start, end)
            except Exception as e:  # noqa: BLE001
                log.warning("%s failed for %s %s: %s", p.name, ticker, tf, e)
                last_err = e
                continue
            if len(df):
                log.info("%s: %s %s -> %d bars", p.name, ticker, tf, len(df))
                return df
        if last_err:
            log.warning("no provider returned data for %s %s (%s)", ticker, tf, last_err)
        return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC"))

    def ensure(self, ticker: str, tf: str, start, end) -> None:
        """Make sure the cache covers [start, end] for a native timeframe."""
        start, end = _ts(start), min(_ts(end), pd.Timestamp.now(tz="UTC"))
        have, cov = self.store.load(ticker, tf)
        slack = self.slack if self.slack is not None else max(tfm.delta(tf), pd.Timedelta(minutes=30))
        gaps = []
        if cov is None:
            gaps.append((start, end))
        else:
            if start < cov[0]:
                gaps.append((start, cov[0]))
            if end - cov[1] > slack:
                gaps.append((cov[1] - pd.Timedelta(days=3), end))
        if not gaps or not self.fetch_missing:
            return
        log.info("data %s %s: fetching %s", ticker, tf, ", ".join(f"{a:%Y-%m-%d %H:%M}->{b:%Y-%m-%d %H:%M}" for a, b in gaps))
        chunks = [c for a, b in gaps for c in _chunks(a, b, tf)]
        frames = [have]
        for k, (a, b) in enumerate(chunks):
            if self.on_chunk:
                self.on_chunk(f"downloading {ticker} {tf} candles", k, len(chunks))
            frames.append(self._fetch(ticker, tf, a, b))
        if self.on_chunk:
            self.on_chunk(f"downloading {ticker} {tf} candles", len(chunks), len(chunks))
        df = pd.concat([f for f in frames if len(f)]) if any(len(f) for f in frames) else have
        df = df[~df.index.duplicated(keep="last")].sort_index()
        # never cache bars that have not finished forming
        df = df[df.index + tfm.delta(tf) <= pd.Timestamp.now(tz="UTC")]
        new_cov = (min(start, cov[0]) if cov else start, max(end, cov[1]) if cov else end)
        self.store.save(ticker, tf, df, new_cov)

    # -- public -----------------------------------------------------------------------------
    def bars(self, ticker: str, tf: str, start, end) -> pd.DataFrame:
        """Bars frame (UTC open-time index, OHLCV + `avail`) for [start, end), built from the best native base."""
        start, end = _ts(start), _ts(end)
        base = tfm.native_base(tf)
        self.ensure(ticker, base, start, end)
        df, _ = self.store.load(ticker, base)
        df = df[(df.index >= start) & (df.index < end)]
        if df.empty:
            raise LookupError(f"no {base} data for {ticker} in [{start.date()}, {end.date()}); "
                              f"check the ticker, dates, and provider keys (see .env.example)")
        out = add_avail(df, tf) if tf == base else resample(df, base, tf)
        return out[out["avail"] <= end]           # only candles that had CLOSED by `end` (matters live)
