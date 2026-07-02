"""
买卖信号评分逻辑（六维因子版）。
输入：compute_indicators 处理后的日线/周线 DataFrame。
输出：SignalResult dataclass。

技术评分六维（信息互补，无量纲）：
  ① 动量    RSI三档 + MACD金叉/柱体 + Stochastic超买超卖
  ② 趋势强度 ADX(14)：唯一衡量趋势"力度"而非方向
  ③ 价格结构 布林带位置 + 收窄（蓄势）
  ④ 短期趋势 MA5/MA20 交叉
  ⑤ 中长期趋势 周线 MA20/MA50 交叉
  ⑥ 量价聪明钱 OBV背离检测

市场数据修正层（volume/资金流/基本面/事件）在 multi_timeframe._market_score() 叠加，
最终强度上限 100。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
import math
import pandas as pd


@dataclass
class SignalResult:
    symbol: str
    direction: str          # "BUY" | "SELL" | "NEUTRAL"
    strength: int           # 0-100
    reasons: list[str]
    close_price: float
    rsi: float
    weekly_trend: str       # "BULLISH" | "BEARISH" | "NEUTRAL"
    current_price: float = float("nan")
    position_usd: float = 0.0
    position_shares: int = 0
    ma200: float = float("nan")
    bb_upper: float = float("nan")
    bb_lower: float = float("nan")
    today_change_pct: float = float("nan")
    pre_change_pct: float = float("nan")
    volume_ratio: float = float("nan")
    pe_ttm: float = float("nan")
    week52_high: float = float("nan")
    week52_low: float = float("nan")
    bid_ask_ratio: float = float("nan")
    next_earnings_date: str = ""
    earnings_history: list = field(default_factory=list)
    net_super_flow: float = float("nan")
    net_big_flow: float = float("nan")
    net_small_flow: float = float("nan")
    pe_percentile: float = float("nan")
    pe_5yr_avg: float = float("nan")
    pe_forward: float = float("nan")
    morningstar_stars: int = 0
    morningstar_fair_value: float = float("nan")
    morningstar_note_title: str = ""
    morningstar_bull: list = field(default_factory=list)
    morningstar_bear: list = field(default_factory=list)
    short_pct: float = float("nan")
    days_to_cover: float = float("nan")
    options_unusual: str = ""
    financial_unusual: str = ""
    technical_unusual: str = ""
    analyst_buy_pct: float = float("nan")
    analyst_target_avg: float = float("nan")
    analyst_target_high: float = float("nan")
    analyst_total: int = 0
    insider_trades: list = field(default_factory=list)
    atr_pct: float = float("nan")
    estimated_gain_pct: float = 0.0
    estimated_gain_raw_pct: float = 0.0
    generated_at: datetime = field(default_factory=datetime.now)
    tier: str = ""
    pinned: bool = False
    ai_analysis: dict = field(default_factory=dict)
    # 附带原始数据供 AI 使用
    daily_df: object = field(default=None, repr=False)
    weekly_df: object = field(default=None, repr=False)
    quote: dict = field(default_factory=dict)
    short_data: dict = field(default_factory=dict)
    analyst: dict = field(default_factory=dict)


def _safe(val) -> float:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return float("nan")
    return float(val)


def _is_valid(*vals) -> bool:
    return all(not math.isnan(_safe(v)) for v in vals)


def _obv_analysis(daily_df: pd.DataFrame) -> tuple[int, list[str], list[str]]:
    """
    OBV 聪明钱方向分析（需要至少 10 日数据）。
    底背离：价格创新低但 OBV 不创新低 → 资金在悄悄买入，强烈看涨。
    顶背离：价格创新高但 OBV 不创新高 → 资金在悄悄出货，强烈看跌。
    返回 (delta_score, pos_reasons, neg_reasons)，正负原因分开传，
    避免调用方把矛盾原因混入同一方向的 reasons 列表。
    """
    if "obv" not in daily_df.columns or len(daily_df) < 10:
        return 0, [], []

    obv_s   = daily_df["obv"].tail(10)
    close_s = daily_df["close"].tail(10)
    score = 0
    pos_reasons: list[str] = []
    neg_reasons: list[str] = []

    last_close = close_s.iloc[-1]
    last_obv   = obv_s.iloc[-1]

    # 底背离（看涨）：价格接近10日低点，但OBV不是最低
    if last_close <= close_s.quantile(0.15) and last_obv > obv_s.min():
        score += 14
        pos_reasons.append("OBV底背离（价格低位但资金未流出，聪明钱在积累）")

    # 顶背离（看跌）：价格接近10日高点，但OBV不是最高
    elif last_close >= close_s.quantile(0.85) and last_obv < obv_s.max():
        score -= 14
        neg_reasons.append("OBV顶背离（价格高位但资金已出逃）")

    # OBV 5日趋势方向（辅助确认）
    obv_5 = obv_s.tail(5)
    if obv_5.iloc[-1] > obv_5.iloc[0] * 1.001:
        score += 5
        pos_reasons.append("OBV 5日持续上升（资金净流入确认）")
    elif obv_5.iloc[-1] < obv_5.iloc[0] * 0.999:
        score -= 5
        neg_reasons.append("OBV 5日持续下降（资金净流出）")

    return score, pos_reasons, neg_reasons


def generate_signal(daily_df: pd.DataFrame, weekly_df: pd.DataFrame, symbol: str = "") -> SignalResult:
    if daily_df.empty:
        return SignalResult(symbol, "NEUTRAL", 0, ["数据不足"], 0.0, float("nan"), "NEUTRAL")

    d  = daily_df.iloc[-1]
    dp = daily_df.iloc[-2] if len(daily_df) >= 2 else daily_df.iloc[-1]

    # ── 提取所有指标值 ──────────────────────────────
    close       = _safe(d.get("close"))
    rsi         = _safe(d.get("rsi"))
    macd        = _safe(d.get("macd"))
    macd_sig    = _safe(d.get("macd_signal"))
    macd_hist   = _safe(d.get("macd_hist"))
    prev_macd   = _safe(dp.get("macd"))
    prev_sig    = _safe(dp.get("macd_signal"))
    prev_hist   = _safe(dp.get("macd_hist"))
    bb_upper    = _safe(d.get("bb_upper"))
    bb_lower    = _safe(d.get("bb_lower"))
    bb_mid      = _safe(d.get("bb_mid"))
    prev_bb_mid = _safe(dp.get("bb_mid"))
    ma5         = _safe(d.get("ma5"))
    prev_ma5    = _safe(dp.get("ma5"))
    atr         = _safe(d.get("atr"))
    adx         = _safe(d.get("adx"))
    stoch_k     = _safe(d.get("stoch_k"))
    stoch_d     = _safe(d.get("stoch_d"))
    prev_stoch_k = _safe(dp.get("stoch_k"))
    prev_stoch_d = _safe(dp.get("stoch_d"))
    roc20       = _safe(d.get("roc20"))
    ma200       = _safe(d.get("ma200"))

    # ── 周线趋势 ────────────────────────────────────
    weekly_trend = "NEUTRAL"
    w_golden_cross = w_death_cross = False
    if not weekly_df.empty and len(weekly_df) >= 2:
        w  = weekly_df.iloc[-1]
        wp = weekly_df.iloc[-2]
        wma20, wma50         = _safe(w.get("ma20")),  _safe(w.get("ma50"))
        prev_wma20, prev_wma50 = _safe(wp.get("ma20")), _safe(wp.get("ma50"))
        if _is_valid(wma20, wma50):
            if wma20 > wma50:
                weekly_trend = "BULLISH"
                if _is_valid(prev_wma20, prev_wma50) and prev_wma20 <= prev_wma50:
                    w_golden_cross = True
            else:
                weekly_trend = "BEARISH"
                if _is_valid(prev_wma20, prev_wma50) and prev_wma20 >= prev_wma50:
                    w_death_cross = True

    # ── OBV 背离（多行分析）─────────────────────────
    obv_delta, obv_pos, obv_neg = _obv_analysis(daily_df)

    # ═══════════════════════════════════════════════
    # BUY 评分
    # ═══════════════════════════════════════════════
    buy_score = 0
    buy_reasons: list[str] = []

    # MA200 门控
    above_ma200 = _is_valid(ma200, close) and close > ma200
    if not above_ma200 and _is_valid(ma200):
        buy_reasons.append(f"价格低于MA200（{ma200:.2f}），不触发买入")

    if above_ma200 or not _is_valid(ma200):

        # ── ① 动量（RSI + MACD + Stochastic，max ~50）──
        if _is_valid(rsi):
            if rsi < 30:
                buy_score += 20
                buy_reasons.append(f"RSI深度超卖 {rsi:.1f}（历史低位买点）")
            elif rsi < 45:
                buy_score += 12
                buy_reasons.append(f"RSI超卖区域 {rsi:.1f}")
            elif rsi <= 55:
                buy_score += 5
                buy_reasons.append(f"RSI中性偏低 {rsi:.1f}")

        if _is_valid(macd, macd_sig, prev_macd, prev_sig):
            if macd > macd_sig and prev_macd <= prev_sig:
                buy_score += 20
                buy_reasons.append("MACD金叉（动能转正）")
            elif _is_valid(macd_hist, prev_hist, atr) and atr > 0:
                # ATR归一化：柱体增量 > 0.5% × ATR 才算有效放大
                if (macd_hist - prev_hist) > atr * 0.005 and macd_hist > 0:
                    buy_score += 8
                    buy_reasons.append("MACD动能持续增强（ATR标准化确认）")

        # Stochastic：与RSI独立的动量维度（用价格区间而非收盘价）
        if _is_valid(stoch_k, stoch_d, prev_stoch_k, prev_stoch_d):
            if stoch_k < 25 and stoch_d < 25:
                if stoch_k > stoch_d and prev_stoch_k <= prev_stoch_d:
                    buy_score += 14
                    buy_reasons.append(f"随机指标超卖区金叉（K={stoch_k:.0f}，与RSI双确认）")
                elif stoch_k < 20:
                    buy_score += 7
                    buy_reasons.append(f"随机指标深度超卖（K={stoch_k:.0f}）")
            elif stoch_k < 35 and stoch_k > stoch_d and prev_stoch_k <= prev_stoch_d:
                buy_score += 6
                buy_reasons.append(f"随机指标低位回升（K={stoch_k:.0f}）")

        # ── ① ROC20 近期价格动能（过去20交易日涨跌幅，直接反映近日情况）──
        if _is_valid(roc20):
            if roc20 > 15:
                buy_score += 8
                buy_reasons.append(f"ROC20 {roc20:+.1f}%，近一个月强势上涨")
            elif roc20 > 5:
                buy_score += 4
                buy_reasons.append(f"ROC20 {roc20:+.1f}%，近期趋势向上")
            elif roc20 < -15:
                buy_score -= 8
                buy_reasons.append(f"ROC20 {roc20:+.1f}%，近一个月持续下跌，追涨需谨慎")
            elif roc20 < -5:
                buy_score -= 4
                buy_reasons.append(f"ROC20 {roc20:+.1f}%，近期走势偏弱")

        # ── ② 趋势强度（ADX，max ±10）──────────────
        if _is_valid(adx):
            if adx > 30:
                buy_score += 10
                buy_reasons.append(f"ADX={adx:.0f}趋势强劲，信号可信度高")
            elif adx > 20:
                buy_score += 4
                buy_reasons.append(f"ADX={adx:.0f}趋势适中")
            elif adx < 15:
                buy_score -= 10
                buy_reasons.append(f"ADX={adx:.0f}趋势极弱（震荡市，慎追）")

        # ── ③ 价格结构（布林带，max ~23）────────────
        if _is_valid(close, bb_lower, bb_mid, bb_upper):
            if close <= bb_lower:
                buy_score += 16
                buy_reasons.append("价格触及布林下轨（极端超卖区）")
            elif close < bb_mid:
                buy_score += 7
                buy_reasons.append("价格从布林下轨区域反弹")
            if bb_mid > 0 and (bb_upper - bb_lower) / bb_mid * 100 < 8:
                buy_score += 5
                buy_reasons.append(f"布林带收窄 {(bb_upper-bb_lower)/bb_mid*100:.1f}%（蓄势突破前兆）")

        # ── ④ 短期趋势（MA5/MA20，max ~15）──────────
        if _is_valid(ma5, bb_mid, prev_ma5, prev_bb_mid):
            if ma5 > bb_mid and prev_ma5 <= prev_bb_mid:
                buy_score += 14
                buy_reasons.append("短期金叉（MA5上穿MA20）")
            elif ma5 > bb_mid:
                buy_score += 6
                buy_reasons.append("短期趋势向上（MA5＞MA20）")

        # ── ⑤ 中长期趋势（周线，max ~22）────────────
        if w_golden_cross:
            buy_score += 20
            buy_reasons.append("周线金叉（MA20上穿MA50）")
        elif weekly_trend == "BULLISH":
            buy_score += 9
            buy_reasons.append("周线趋势看涨（MA20＞MA50）")

        # ── ⑥ OBV量价聪明钱（独立于价格趋势）────────
        if obv_delta > 0:
            buy_score += obv_delta
            buy_reasons.extend(obv_pos)   # 只添加正面原因，负面已体现在 delta 折扣里

    # ═══════════════════════════════════════════════
    # SELL 评分
    # ═══════════════════════════════════════════════
    sell_score = 0
    sell_reasons: list[str] = []

    # ── ① 动量 ────────────────────────────────────
    if _is_valid(rsi):
        if rsi > 80:
            sell_score += 35
            sell_reasons.append(f"RSI极度超买 {rsi:.1f}（高位回调风险大）")
        elif rsi > 70:
            sell_score += 22
            sell_reasons.append(f"RSI超买区域 {rsi:.1f}")
        elif rsi > 65:
            sell_score += 9
            sell_reasons.append(f"RSI偏高 {rsi:.1f}，注意过热")

    if _is_valid(macd, macd_sig, prev_macd, prev_sig):
        if macd < macd_sig and prev_macd >= prev_sig:
            sell_score += 20
            sell_reasons.append("MACD死叉（动能转负）")
        elif _is_valid(macd_hist, prev_hist, atr) and atr > 0:
            if (macd_hist - prev_hist) < -atr * 0.005 and prev_hist > 0:
                sell_score += 8
                sell_reasons.append("MACD动能持续衰退（ATR标准化确认）")

    if _is_valid(stoch_k, stoch_d, prev_stoch_k, prev_stoch_d):
        if stoch_k > 75 and stoch_d > 75:
            if stoch_k < stoch_d and prev_stoch_k >= prev_stoch_d:
                sell_score += 14
                sell_reasons.append(f"随机指标超买区死叉（K={stoch_k:.0f}）")
            elif stoch_k > 80:
                sell_score += 7
                sell_reasons.append(f"随机指标极度超买（K={stoch_k:.0f}）")
        elif stoch_k > 65 and stoch_k < stoch_d and prev_stoch_k >= prev_stoch_d:
            sell_score += 6
            sell_reasons.append(f"随机指标高位回落（K={stoch_k:.0f}）")

    # ── ① ROC20 近期价格动能 ──────────────────────────
    if _is_valid(roc20):
        if roc20 < -15:
            sell_score += 8
            sell_reasons.append(f"ROC20 {roc20:+.1f}%，近一个月强势下跌，下行趋势确认")
        elif roc20 < -5:
            sell_score += 4
            sell_reasons.append(f"ROC20 {roc20:+.1f}%，近期走势偏弱")
        elif roc20 > 15:
            sell_score -= 6
            sell_reasons.append(f"ROC20 {roc20:+.1f}%，近期强势上涨，卖出需谨慎（可能仍强）")

    # ── ② 趋势强度 ────────────────────────────────
    if _is_valid(adx):
        if adx > 30:
            sell_score += 10
            sell_reasons.append(f"ADX={adx:.0f}趋势强劲，下跌动能强")
        elif adx < 15:
            sell_score -= 10
            sell_reasons.append(f"ADX={adx:.0f}趋势极弱，卖出信号不可靠")

    # ── ③ 价格结构 ────────────────────────────────
    if _is_valid(close, bb_upper):
        if close >= bb_upper:
            sell_score += 15
            sell_reasons.append("价格突破布林上轨（极端超买区）")
        elif _is_valid(bb_mid) and close > bb_mid * 1.05:
            sell_score += 6
            sell_reasons.append("价格明显偏离布林中轨上方")

    # ── ④ 短期趋势 ────────────────────────────────
    if _is_valid(ma5, bb_mid, prev_ma5, prev_bb_mid):
        if ma5 < bb_mid and prev_ma5 >= prev_bb_mid:
            sell_score += 12
            sell_reasons.append("短期死叉（MA5下穿MA20）")
        elif ma5 < bb_mid:
            sell_score += 5
            sell_reasons.append("短期趋势向下（MA5＜MA20）")

    # ── ⑤ 中长期趋势 ─────────────────────────────
    if w_death_cross:
        sell_score += 20
        sell_reasons.append("周线死叉（MA20下穿MA50）")
    elif weekly_trend == "BEARISH":
        sell_score += 9
        sell_reasons.append("周线趋势看跌（MA20＜MA50）")

    # ── ⑥ OBV量价聪明钱 ─────────────────────────
    if obv_delta < 0:
        sell_score += abs(obv_delta)
        sell_reasons.extend(obv_neg)   # 只添加负面原因

    # ═══════════════════════════════════════════════
    # 最终判定
    # ═══════════════════════════════════════════════
    if buy_score >= 40 and buy_score > sell_score + 15:
        direction = "BUY"
        strength  = min(buy_score, 100)
        reasons   = buy_reasons
    elif sell_score >= 40 and sell_score > buy_score + 15:
        direction = "SELL"
        strength  = min(sell_score, 100)
        reasons   = sell_reasons
    else:
        direction = "NEUTRAL"
        strength  = min(max(buy_score, sell_score), 100)
        reasons   = (buy_reasons + sell_reasons) or ["指标无明显信号"]

    return SignalResult(
        symbol=symbol,
        direction=direction,
        strength=strength,
        reasons=reasons,
        close_price=close,
        rsi=rsi,
        weekly_trend=weekly_trend,
        ma200=ma200,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
    )
