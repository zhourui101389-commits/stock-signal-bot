"""
技术指标计算模块（纯函数，无副作用）。
输入：OHLCV DataFrame，索引为日期。
输出：附加指标列的 DataFrame 副本。

指标分类（按视频铁律二"丰富度"原则，跨类别信息互补）：
  动量类:  RSI(14), Stochastic K/D(14,3)
  趋势类:  MACD, MA5/MA20/MA50/MA200
  价格结构: BollingerBands(20,2)
  趋势强度: ADX(14)       ← 唯一衡量趋势"强弱"而非"方向"的因子
  量价类:  OBV            ← 成交量累积方向，与价格独立
  动量速度: ROC(20)        ← 纯价格变化率，无量纲百分比
  波动率:  ATR(14)
"""
import pandas as pd
import ta.momentum
import ta.trend
import ta.volatility
import ta.volume


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 26:
        return df

    df = df.copy()
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    macd = ta.trend.MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
    bb   = ta.volatility.BollingerBands(close=close, window=20, window_dev=2)

    # ── 动量类 ────────────────────────────────────
    stoch = ta.momentum.StochasticOscillator(
        high=high, low=low, close=close, window=14, smooth_window=3
    )
    indicators = {
        "rsi":       ta.momentum.RSIIndicator(close=close, window=14).rsi(),
        "stoch_k":   stoch.stoch(),           # %K：0-100，无量纲
        "stoch_d":   stoch.stoch_signal(),    # %D：K的3日平滑，0-100
    }

    # ── 趋势类 ────────────────────────────────────
    indicators.update({
        "macd":        macd.macd(),
        "macd_hist":   macd.macd_diff(),
        "macd_signal": macd.macd_signal(),
        "ma5":         ta.trend.SMAIndicator(close=close, window=5).sma_indicator(),
        "ma20":        ta.trend.SMAIndicator(close=close, window=20).sma_indicator(),
        "ma50":        ta.trend.SMAIndicator(close=close, window=50).sma_indicator(),
    })

    # ── 价格结构 ──────────────────────────────────
    indicators.update({
        "bb_lower": bb.bollinger_lband(),
        "bb_mid":   bb.bollinger_mavg(),
        "bb_upper": bb.bollinger_hband(),
    })

    # ── 趋势强度（ADX：唯一衡量趋势力度而非方向，0-100 无量纲）──
    indicators["adx"] = ta.trend.ADXIndicator(
        high=high, low=low, close=close, window=14
    ).adx()

    # ── 量价类（OBV：价升量增/价跌量减的累积，与价格趋势独立）──
    indicators["obv"] = ta.volume.OnBalanceVolumeIndicator(
        close=close, volume=volume
    ).on_balance_volume()

    # ── 动量速度（ROC 20日：纯变化率，无量纲百分比）─────────────
    if len(df) >= 20:
        indicators["roc20"] = ta.momentum.ROCIndicator(close=close, window=20).roc()

    # ── 波动率 ────────────────────────────────────
    indicators["atr"] = ta.volatility.AverageTrueRange(
        high=high, low=low, close=close, window=14
    ).average_true_range()

    if len(df) >= 200:
        indicators["ma200"] = ta.trend.SMAIndicator(close=close, window=200).sma_indicator()

    return pd.concat([df, pd.DataFrame(indicators, index=df.index)], axis=1)
