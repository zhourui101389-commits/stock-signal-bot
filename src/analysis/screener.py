"""
市场初筛：从更大的候选池（标普500 + 中概ADR）里用便宜的纯技术面指标
（动量 + 成交量确认）批量打分，选出 Top N 只喂给现有的 AI 深度分析流程。
不调用 AI，只做一次批量行情下载，控制扫描成本。

叠加两层非纯技术面过滤，避免只看动量/成交量的盲区：
  - VIX情绪状态：恐慌区间收紧候选（只留正20日动量的，且入选数减半）
  - 财报临近排除：未来N天内公布财报的候选直接剔除，避免括号单被财报跳空打穿
"""
from __future__ import annotations

import datetime
import logging

import pandas as pd

logger = logging.getLogger(__name__)

_MIN_PRICE = 5.0        # 过滤低价股，减少异常波动/流动性风险
_LOOKBACK_DAYS = "60d"  # 批量下载窗口，覆盖20日动量+20日均量计算
_VIX_PANIC_LEVEL = 25.0  # 与 cloud_scan._get_macro_context 的"恐慌"档位一致
_EARNINGS_EXCLUDE_DAYS = 3  # 未来N个日历日内有财报的候选直接排除


def _score_symbol(close: pd.Series, volume: pd.Series) -> tuple[float, float] | None:
    """动量+成交量确认打分：5日涨幅 + 0.5×20日涨幅 + 成交量放大加成。返回 (score, mom_20d)。"""
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

    score = mom_5d + 0.5 * mom_20d + max(0.0, vol_ratio - 1) * 10
    return score, mom_20d


def _get_vix_level() -> float | None:
    """当前VIX收盘价，获取失败时返回None（调用方应视为"正常"档位，不阻断主流程）。"""
    import yfinance as yf
    try:
        hist = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1].item() if hasattr(hist["Close"].iloc[-1], "item")
                     else hist["Close"].iloc[-1])
    except Exception as e:
        logger.debug("VIX获取失败: %s", e)
        return None


def _get_earnings_exclusions(finnhub_client, days: int = _EARNINGS_EXCLUDE_DAYS) -> set[str]:
    """未来N天内公布财报的股票代码集合。finnhub_client为None或调用失败时返回空集（不排除）。"""
    if finnhub_client is None:
        return set()
    try:
        today = datetime.date.today()
        end   = today + datetime.timedelta(days=days)
        return finnhub_client.get_earnings_calendar(str(today), str(end))
    except Exception as e:
        logger.debug("财报日历获取失败: %s", e)
        return set()


def screen_top_candidates(
    universe: list[str],
    top_n: int = 25,
    exclude: set[str] | None = None,
    finnhub_client=None,
) -> list[str]:
    """
    对 universe 批量打分，返回得分最高的 top_n 个代码（已排除 exclude 中的symbol）。
    纯技术面初筛为主，叠加VIX情绪状态和财报临近两层过滤。失败时返回空列表（不影响主流程）。
    """
    import yfinance as yf

    exclude = exclude or set()
    earnings_soon = _get_earnings_exclusions(finnhub_client)
    candidates = [s for s in universe if s not in exclude and s not in earnings_soon]
    if not candidates:
        return []

    vix = _get_vix_level()
    panic = vix is not None and vix >= _VIX_PANIC_LEVEL
    effective_top_n = max(1, top_n // 2) if panic else top_n

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
            result = _score_symbol(close_df[sym], volume_df[sym])
        except Exception as e:
            logger.debug("初筛打分异常 %s: %s", sym, e)
            continue
        if result is None:
            continue
        score, mom_20d = result
        if panic and mom_20d <= 0:
            continue  # 恐慌区间：只留20日动量为正的，不逆势接刀
        scored.append((sym, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    top = [sym for sym, _ in scored[:effective_top_n]]
    logger.info(
        "市场初筛：候选池%d只（财报临近排除%d只）→ VIX=%s%s → 有效打分%d只 → 入选Top%d: %s",
        len(candidates), len(earnings_soon),
        f"{vix:.1f}" if vix is not None else "N/A",
        "（恐慌，收紧）" if panic else "",
        len(scored), effective_top_n, ", ".join(top),
    )
    return top
