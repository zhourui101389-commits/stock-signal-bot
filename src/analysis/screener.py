"""
市场初筛：从更大的候选池（标普500 + 中概ADR）里用便宜的纯技术面指标
（动量 + 成交量确认）批量打分，选出 Top N 只喂给现有的 AI 深度分析流程。
不调用 AI，只做一次批量行情下载，控制扫描成本。
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

_MIN_PRICE = 5.0        # 过滤低价股，减少异常波动/流动性风险
_LOOKBACK_DAYS = "60d"  # 批量下载窗口，覆盖20日动量+20日均量计算


def _score_symbol(close: pd.Series, volume: pd.Series) -> float | None:
    """动量+成交量确认打分：5日涨幅 + 0.5×20日涨幅 + 成交量放大加成。"""
    close = close.dropna()
    volume = volume.dropna()
    if len(close) < 21 or len(volume) < 21:
        return None
    last_price = float(close.iloc[-1])
    if last_price < _MIN_PRICE:
        return None

    mom_5d  = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0.0
    mom_20d = (close.iloc[-1] / close.iloc[-21] - 1) * 100

    avg_vol_20 = volume.iloc[-21:-1].mean()
    today_vol  = float(volume.iloc[-1])
    vol_ratio  = today_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0

    return mom_5d + 0.5 * mom_20d + max(0.0, vol_ratio - 1) * 10


def screen_top_candidates(
    universe: list[str],
    top_n: int = 25,
    exclude: set[str] | None = None,
) -> list[str]:
    """
    对 universe 批量打分，返回得分最高的 top_n 个代码（已排除 exclude 中的symbol）。
    纯技术面初筛，不调用AI，失败时返回空列表（不影响主流程）。
    """
    import yfinance as yf

    exclude = exclude or set()
    candidates = [s for s in universe if s not in exclude]
    if not candidates:
        return []

    try:
        df = yf.download(candidates, period=_LOOKBACK_DAYS, interval="1d",
                         progress=False, auto_adjust=True, group_by="column")
    except Exception as e:
        logger.warning("初筛批量下载行情失败: %s", e)
        return []

    if df.empty:
        return []

    try:
        close_df  = df["Close"]
        volume_df = df["Volume"]
    except Exception as e:
        logger.warning("初筛行情列缺失: %s", e)
        return []

    if len(candidates) == 1:
        close_df  = close_df.to_frame(candidates[0])
        volume_df = volume_df.to_frame(candidates[0])

    scored: list[tuple[str, float]] = []
    for sym in candidates:
        if sym not in close_df.columns or sym not in volume_df.columns:
            continue
        try:
            score = _score_symbol(close_df[sym], volume_df[sym])
        except Exception as e:
            logger.debug("初筛打分异常 %s: %s", sym, e)
            continue
        if score is not None:
            scored.append((sym, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    top = [sym for sym, _ in scored[:top_n]]
    logger.info("市场初筛：候选池%d只 → 有效打分%d只 → 入选Top%d: %s",
                len(candidates), len(scored), top_n, ", ".join(top))
    return top
