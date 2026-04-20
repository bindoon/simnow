from __future__ import annotations

import argparse

import pandas as pd
import yfinance as yf


DEFAULT_SYMBOL = "AAPL"
DEFAULT_DAYS = 10
DEFAULT_LOOKBACK = "1mo"


def fetch_recent_history(symbol: str, days: int) -> pd.DataFrame:
    history = yf.Ticker(symbol).history(
        period=DEFAULT_LOOKBACK,
        interval="1d",
        auto_adjust=False,
    )
    if history.empty:
        raise RuntimeError(f"未拉到 {symbol} 的历史数据")

    frame = history.reset_index()
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d")
    frame = frame[["Date", "Open", "High", "Low", "Close", "Volume"]].tail(days).copy()
    frame[["Open", "High", "Low", "Close"]] = frame[["Open", "High", "Low", "Close"]].round(2)
    return frame


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="拉取股票最近 N 个交易日的日线数据")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="股票代码，默认 AAPL")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="拉取最近多少个交易日，默认 10")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    frame = fetch_recent_history(args.symbol, args.days)

    print(f"股票代码: {args.symbol}")
    print(f"展示最近 {args.days} 个交易日数据")
    print(frame.to_string(index=False))


if __name__ == "__main__":
    main()