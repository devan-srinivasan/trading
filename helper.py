import yfinance as yf
import numpy as np
import pandas as pd
import datetime as dt
import vectorbt as vbt
from tqdm import tqdm

def get_data(tickers, start, end):
    tickers = [tickers] if isinstance(tickers, str) else list(tickers)

    df = yf.download(tickers, start=start, end=end, group_by="ticker", auto_adjust=False)

    # If a single ticker is requested, yfinance returns single-level columns
    if len(tickers) == 1:
        t = tickers[0]
        df.columns = pd.MultiIndex.from_product([[t], df.columns])

    # Keep only Close and Volume for each ticker
    keep_fields = {"Close", "Volume"}
    df = df.loc[:, df.columns.get_level_values(1).isin(keep_fields)]

    df = df.reset_index().rename(columns={"index": "Date"})
    return df

class Portfolio:
    def __init__(self, tickers, start, end):
        self.tickers = tickers
        self.start = start
        self.end = end
        self.data = get_data(tickers, start, end)

    
portfolio = Portfolio(tickers=["AAPL", "MSFT", "GOOGL"], start="2020-01-01", end="2025-12-31")
print(portfolio.data.head())