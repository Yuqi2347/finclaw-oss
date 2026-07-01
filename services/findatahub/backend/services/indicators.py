from __future__ import annotations

import pandas as pd


def calculate_indicators(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()

    df = daily.sort_values("trade_date").copy()
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    volume = pd.to_numeric(df.get("volume"), errors="coerce")

    df["ma5"] = close.rolling(5).mean()
    df["ma10"] = close.rolling(10).mean()
    df["ma20"] = close.rolling(20).mean()
    df["ma60"] = close.rolling(60).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    df["rsi14"] = 100 - (100 / (1 + rs))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    boll_mid = close.rolling(20).mean()
    boll_std = close.rolling(20).std()
    df["boll_mid"] = boll_mid
    df["boll_ub"] = boll_mid + 2 * boll_std
    df["boll_lb"] = boll_mid - 2 * boll_std
    df["vwma20"] = (close * volume).rolling(20).sum() / volume.rolling(20).sum()

    return df[
        [
            "ticker",
            "trade_date",
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "rsi14",
            "macd",
            "macd_signal",
            "macd_hist",
            "atr14",
            "boll_mid",
            "boll_ub",
            "boll_lb",
            "vwma20",
        ]
    ]
