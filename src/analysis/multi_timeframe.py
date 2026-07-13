"""
多周期分析编排：K线 → 技术指标 → 信号评分 → 实时行情 → 今日涨幅预估。
"""
import logging
import math
from src.data.moomoo_client import MoomooDataClient
from src.analysis.indicators import compute_indicators
from src.analysis.signals import SignalResult, generate_signal

logger = logging.getLogger(__name__)

_NAN = float("nan")


def _market_score(result: SignalResult) -> tuple[int, list[str]]:
    """
    市场数据评分层 — 将所有实时/基本面数据转化为评分修正。

    八大类别（技术层之外的独立信息维度）：
      ④ 量价确认      volume_ratio
      ⑤ 资金流向      net_super_flow / net_big_flow
      ⑥ 机构共识      analyst_buy_pct + 目标价上涨空间
      ⑦ 估值位置      pe_percentile + Morningstar
      ⑧ 事件风险      财报邻近 / 异动信号 / 期权异动
      ⑨ 内部人士信号  insider_trades（高管真金白银的判断）
      ⑩ 空头结构      short_pct / days_to_cover（压力 or 轧空潜力）
      ⑪ 盘前确认      pre_change_pct（非对称：背离重罚，对齐轻奖）
    """
    import datetime as _dt

    delta = 0
    reasons: list[str] = []
    d = result.direction

    # ── ④ 量价确认 ─────────────────────────────────
    vr = result.volume_ratio
    if not math.isnan(vr) and vr > 0:
        if vr >= 2.5:
            delta += 20
            reasons.append(f"量比 {vr:.1f}x 爆量，强力确认")
        elif vr >= 1.8:
            delta += 14
            reasons.append(f"量比 {vr:.1f}x 明显放量")
        elif vr >= 1.3:
            delta += 8
            reasons.append(f"量比 {vr:.1f}x 温和放量")
        elif vr < 0.6:
            delta -= 12
            reasons.append(f"量比 {vr:.1f}x 严重缩量，信号可信度低")
        elif vr < 0.8:
            delta -= 6
            reasons.append(f"量比 {vr:.1f}x 缩量，谨慎")

    # ── ⑤ 资金流向 ─────────────────────────────────
    nsf = result.net_super_flow   # 百万USD
    nbf = result.net_big_flow
    if not math.isnan(nsf):
        if d == "BUY":
            if nsf > 50:
                delta += 15
                reasons.append(f"超大单净流入 ${nsf:.0f}M（大额订单买盘占优）")
            elif nsf > 15:
                delta += 8
                reasons.append(f"超大单净流入 ${nsf:.0f}M")
            elif nsf > 0 and not math.isnan(nbf) and nbf > 0:
                delta += 4
                reasons.append("大单资金净流入")
            elif nsf < -30:
                delta -= 10
                reasons.append(f"超大单净流出 ${abs(nsf):.0f}M，大额卖盘背离买入信号")
        elif d == "SELL":
            if nsf < -30:
                delta += 15
                reasons.append(f"超大单净流出 ${abs(nsf):.0f}M（大额订单卖盘占优）")
            elif nsf < -10:
                delta += 8
                reasons.append(f"超大单净流出 ${abs(nsf):.0f}M")
            elif nsf > 50:
                delta -= 10
                reasons.append(f"超大单仍在净流入 ${nsf:.0f}M，卖出信号存疑")

    # ── ⑥ 机构共识 ─────────────────────────────────
    abp    = result.analyst_buy_pct
    atotal = result.analyst_total
    if not math.isnan(abp) and atotal >= 5:
        if d == "BUY":
            if abp >= 80:
                delta += 10
                reasons.append(f"分析师 {abp:.0f}% 看涨（{atotal}人）")
            elif abp >= 65:
                delta += 5
                reasons.append(f"分析师 {abp:.0f}% 看涨")
            elif abp < 40:
                delta -= 10
                reasons.append(f"分析师仅 {abp:.0f}% 看涨，机构不认可")
        elif d == "SELL":
            if abp < 40:
                delta += 8
                reasons.append(f"分析师仅 {abp:.0f}% 看涨，佐证卖出")

    # 分析师目标价上涨空间（目标价 vs 当前价）
    price    = result.current_price
    tgt_avg  = result.analyst_target_avg
    tgt_high = result.analyst_target_high
    if not math.isnan(price) and price > 0 and not math.isnan(tgt_avg) and tgt_avg > 0:
        upside = (tgt_avg / price - 1) * 100
        if d == "BUY":
            if upside >= 25:
                delta += 10
                reasons.append(f"分析师均价目标上涨空间 +{upside:.0f}%")
            elif upside >= 15:
                delta += 6
                reasons.append(f"分析师均价目标上涨空间 +{upside:.0f}%")
            elif upside < 5:
                delta -= 6
                reasons.append(f"分析师目标价仅高出 {upside:.0f}%，上涨空间有限")

    # ── ⑦ 估值位置 ─────────────────────────────────
    pep = result.pe_percentile
    if not math.isnan(pep):
        if d == "BUY":
            if pep < 20:
                delta += 10
                reasons.append(f"PE历史低位 {pep:.0f}分位，估值吸引")
            elif pep < 40:
                delta += 5
                reasons.append(f"PE中低位 {pep:.0f}分位")
            elif pep > 85:
                delta -= 10
                reasons.append(f"PE历史高位 {pep:.0f}分位，追高风险大")
            elif pep > 70:
                delta -= 5
                reasons.append(f"PE偏高 {pep:.0f}分位")
        elif d == "SELL" and pep > 85:
            delta += 6
            reasons.append(f"PE历史高位 {pep:.0f}分位，估值过热佐证卖出")

    ms = result.morningstar_stars
    if ms >= 4 and d == "BUY":
        delta += 8
        reasons.append(f"Morningstar {ms}星，低估值买点")
    elif ms in (1, 2) and d == "BUY":
        delta -= 5
        reasons.append(f"Morningstar {ms}星，估值偏高")

    # 52周位置（突破历史高位 = 趋势确认；靠近历史低位 = 下行空间有限）
    w52h = result.week52_high
    w52l = result.week52_low
    if not math.isnan(w52h) and not math.isnan(w52l) and not math.isnan(price):
        if w52h > w52l > 0:
            w52_pct = (price - w52l) / (w52h - w52l) * 100
            if d == "BUY":
                if w52_pct > 90:
                    delta += 12
                    reasons.append(f"突破52周高位区间（位置 {w52_pct:.0f}%），强势趋势")
                elif w52_pct < 20:
                    delta += 8
                    reasons.append(f"靠近52周低位企稳（位置 {w52_pct:.0f}%），下行空间有限")
            elif d == "SELL":
                if w52_pct > 88:
                    delta += 8
                    reasons.append(f"52周高位 {w52_pct:.0f}%，历史压力位，回调空间大")
                elif w52_pct < 10:
                    delta -= 8
                    reasons.append(f"接近52周低位，卖出信号谨慎（可能超卖）")

    # ── ⑧ 事件风险 ─────────────────────────────────
    # 财报日期邻近：不确定性急剧上升，调低仓位置信度
    if result.next_earnings_date:
        try:
            today = _dt.date.today()
            ed    = _dt.date.fromisoformat(result.next_earnings_date[:10])
            days  = (ed - today).days
            if 0 <= days <= 3:
                delta -= 22
                reasons.append(f"⚠️ 财报仅 {days} 天后，建议减半仓或观望")
            elif days <= 7:
                delta -= 14
                reasons.append(f"财报 {days} 天后，注意仓位控制")
            elif days <= 14:
                delta -= 7
                reasons.append(f"财报 {days} 天后，酌情减仓")
        except (ValueError, TypeError):
            pass

    # 历史财报连续超预期（基本面动量）
    if result.earnings_history:
        beats = 0
        for h in result.earnings_history[:4]:   # 最近4季
            if isinstance(h, dict):
                actual = h.get("actual_eps") or h.get("actual")
                est    = h.get("estimated_eps") or h.get("estimate")
                try:
                    if float(actual) > float(est):
                        beats += 1
                except (TypeError, ValueError):
                    pass
        if beats >= 3 and d == "BUY":
            delta += 10
            reasons.append(f"近4季连续 {beats} 次财报超预期，基本面动量强")
        elif beats == 0 and d == "BUY":
            delta -= 5
            reasons.append("近期财报持续不及预期，基本面压力")

    # 异动信号（Moomoo 自有）
    if result.options_unusual:
        if d == "BUY":
            delta += 10
            reasons.append(f"期权异动：{result.options_unusual[:30]}")
        elif d == "SELL":
            delta += 8
            reasons.append(f"期权异动（看跌方向）：{result.options_unusual[:30]}")

    if result.technical_unusual:
        delta += 6
        reasons.append(f"技术异动：{result.technical_unusual[:30]}")

    if result.financial_unusual:
        delta += 5
        reasons.append(f"财务异动：{result.financial_unusual[:30]}")

    # ── ⑨ 内部人士信号 ─────────────────────────────
    if result.insider_trades:
        # moomoo_client 存的字段是 "action"（买入/卖出），兼容 "direction"
        def _insider_dir(t: dict) -> str:
            return str(t.get("action") or t.get("direction") or "").strip()
        buy_cnt  = sum(1 for t in result.insider_trades
                       if isinstance(t, dict) and _insider_dir(t) in ("买入", "BUY", "PURCHASE"))
        sell_cnt = sum(1 for t in result.insider_trades
                       if isinstance(t, dict) and _insider_dir(t) in ("卖出", "SELL", "SALE"))
        if buy_cnt >= 2 and d == "BUY":
            delta += 12
            reasons.append(f"近期高管买入 {buy_cnt} 笔，内部人士真金白银看好")
        elif buy_cnt >= 1 and d == "BUY":
            delta += 6
            reasons.append(f"近期高管买入 {buy_cnt} 笔")
        if sell_cnt >= 3 and d == "BUY":
            delta -= 10
            reasons.append(f"近期高管卖出 {sell_cnt} 笔，警惕内部人离场")
        elif sell_cnt >= 2 and d == "BUY":
            delta -= 5
            reasons.append(f"近期高管卖出 {sell_cnt} 笔")

    # ── ⑩ 空头结构 ─────────────────────────────────
    sp  = result.short_pct
    dtc = result.days_to_cover
    if not math.isnan(sp):
        if d == "BUY":
            if sp > 20:
                delta -= 8
                reasons.append(f"空头仓位 {sp:.0f}%，做空压力较大")
            elif sp > 15:
                delta -= 4
                reasons.append(f"空头仓位 {sp:.0f}%")
            # 高空头 + 买入信号 = 潜在轧空（short squeeze）
            if sp > 15 and not math.isnan(dtc) and dtc > 5:
                delta += 8
                reasons.append(f"空头仓位高 ({sp:.0f}%) + 回补需 {dtc:.0f}天，轧空潜力")
        elif d == "SELL" and sp > 25:
            delta += 6
            reasons.append(f"空头仓位已高达 {sp:.0f}%，继续看跌确认")

    # ── ⑪ 盘前确认（非对称风险过滤）─────────────────
    # 盘前量极薄，对齐=小奖；背离=重罚（隔夜可能有未知风险）
    pre = result.pre_change_pct
    if not math.isnan(pre) and abs(pre) >= 2.0:
        aligned = (pre > 0) == (d == "BUY")
        if not aligned:
            if abs(pre) >= 4:
                delta -= 15
                reasons.append(f"盘前 {pre:+.1f}% 强烈背离技术信号，可能存在隔夜风险事件")
            else:
                delta -= 8
                reasons.append(f"盘前 {pre:+.1f}% 与信号方向相反，谨慎追单")
        else:
            if abs(pre) >= 5:
                delta += 5
                reasons.append(f"盘前 {pre:+.1f}% 确认方向（盘前量薄，仅作参考）")
            else:
                delta += 3
                reasons.append(f"盘前 {pre:+.1f}% 小幅验证信号方向")

    return delta, reasons


def _nan(v) -> float:
    try:
        f = float(v)
        return f if not math.isnan(f) else _NAN
    except (TypeError, ValueError):
        return _NAN


def _estimate_gain(
    direction: str,
    strength: int,
    atr_pct: float,
    pre_change_pct: float,
    volume_ratio: float,
    bid_ask_ratio: float,
) -> tuple[float, float]:
    """
    今日涨幅预估算法（仅供参考）：

    基础 = ATR% × 信号强度
        ATR(14) 是过去14天每日波幅均值，代表"这只股票典型一天能动多少"。
        信号强度（0-1）决定我们对方向的把握程度。

    修正1 盘前动量：
        盘前涨跌方向与信号一致 → 放大预估；相反 → 缩小。
        权重 30%，上限 ±50% 调整。

    修正2 量比：
        量比 > 1.5 说明放量，价格波动往往更大 → 最多 +30%。
        量比 < 0.7 说明缩量，可能磨盘 → -20%。

    修正3 买卖盘比（bid_ask_ratio）：
        正值 = 买盘更强，对 BUY 信号加分；负值 = 卖盘强，对 BUY 信号减分。

    保守折扣 0.55：技术分析本质上是概率性的，大部分情况下实际涨幅
    小于理论预估，所以整体乘以 0.55 作为保守因子。
    """
    if direction == "NEUTRAL" or math.isnan(atr_pct) or atr_pct <= 0:
        return 0.0, 0.0

    sign = 1.0 if direction == "BUY" else -1.0
    base = atr_pct * (strength / 100)

    # 修正1：盘前动量
    pre_factor = 1.0
    if not math.isnan(pre_change_pct):
        aligned = (pre_change_pct > 0) == (direction == "BUY")
        impact = min(abs(pre_change_pct) / 5, 0.5)  # 每5%盘前幅度 = 50%权重上限
        pre_factor = 1.0 + (impact if aligned else -impact) * 0.3

    # 修正2：量比
    vol_factor = 1.0
    if not math.isnan(volume_ratio) and volume_ratio > 0:
        if volume_ratio >= 1.5:
            vol_factor = 1.0 + min((volume_ratio - 1) * 0.2, 0.3)
        elif volume_ratio < 0.7:
            vol_factor = 0.8

    # 修正3：买卖盘
    bab_factor = 1.0
    if not math.isnan(bid_ask_ratio):
        aligned_bab = (bid_ask_ratio > 0) == (direction == "BUY")
        bab_factor = 1.0 + (abs(bid_ask_ratio) / 200) * (0.1 if aligned_bab else -0.1)

    raw = base * pre_factor * vol_factor * bab_factor * sign
    conservative = raw * 0.55
    return round(conservative, 2), round(raw, 2)


def analyze_symbol(
    client: MoomooDataClient,
    symbol: str,
    lookback_days: int = 250,
    lookback_weeks: int = 104,
    total_capital: float = 10000,
    max_position_pct: float = 0.10,
) -> SignalResult:
    logger.info("分析 %s ...", symbol)

    daily_raw = client.get_daily_bars(symbol, limit=lookback_days)
    weekly_raw = client.get_weekly_bars(symbol, limit=lookback_weeks)

    daily_df = compute_indicators(daily_raw)
    weekly_df = compute_indicators(weekly_raw)

    result = generate_signal(daily_df, weekly_df, symbol=symbol)

    # 保存原始数据供 AI 分析使用
    result.daily_df  = daily_df
    result.weekly_df = weekly_df

    # ATR 百分比
    atr_pct = _NAN
    if not daily_df.empty and "atr" in daily_df.columns:
        atr_val = _nan(daily_df["atr"].iloc[-1])
        prev_close = _nan(daily_df["close"].iloc[-1])
        if not math.isnan(atr_val) and not math.isnan(prev_close) and prev_close > 0:
            atr_pct = atr_val / prev_close * 100
    result.atr_pct = atr_pct

    # 实时行情（Moomoo snapshot）
    quote = client.get_quote(symbol)
    current_price = _nan(quote.get("latest_price"))
    if math.isnan(current_price):
        current_price = result.close_price
    result.current_price      = current_price
    result.today_change_pct   = _nan(quote.get("today_change_pct"))
    result.pre_change_pct     = _nan(quote.get("pre_change_pct"))
    result.volume_ratio       = _nan(quote.get("volume_ratio"))
    result.pe_ttm             = _nan(quote.get("pe_ttm"))
    result.week52_high        = _nan(quote.get("week52_high"))
    result.week52_low         = _nan(quote.get("week52_low"))
    result.bid_ask_ratio      = _nan(quote.get("bid_ask_ratio"))

    # ── 财报日期 ──
    earnings = client.get_earnings_summary(symbol)
    if earnings:
        result.next_earnings_date = earnings.get("next_date", "")
        result.earnings_history   = earnings.get("history", [])

    # ── 资金流向 ──
    cap = client.get_capital_flow_summary(symbol)
    if cap:
        result.net_super_flow = _nan(cap.get("net_super")) / 1e6   # 转为百万USD
        result.net_big_flow   = _nan(cap.get("net_big"))   / 1e6
        result.net_small_flow = _nan(cap.get("net_small")) / 1e6

    # ── 估值水位 ──
    val = client.get_valuation_summary(symbol)
    if val:
        result.pe_percentile = _nan(val.get("pe_percentile"))
        result.pe_5yr_avg    = _nan(val.get("pe_5yr_avg"))
        result.pe_forward    = _nan(val.get("pe_forward"))

    # ── Morningstar ──
    ms = client.get_morningstar_summary(symbol)
    if ms:
        result.morningstar_stars      = int(ms.get("stars") or 0)
        result.morningstar_fair_value = _nan(ms.get("fair_value"))
        result.morningstar_note_title = ms.get("note_title", "")
        result.morningstar_bull       = ms.get("bull_says", [])
        result.morningstar_bear       = ms.get("bear_says", [])

    # ── 做空数据 ──
    short = client.get_short_summary(symbol)
    if short:
        result.short_pct      = _nan(short.get("short_pct"))
        result.days_to_cover  = _nan(short.get("days_to_cover"))
        result.short_data     = short

    # ── 期权/财务/技术异动 ──
    unusual = client.get_unusual_signals(symbol)
    if unusual:
        result.options_unusual   = unusual.get("options", "")
        result.financial_unusual = unusual.get("financial", "")
        result.technical_unusual = unusual.get("technical", "")

    # ── 分析师共识 + 内部人士交易 ──
    consensus = client.get_analyst_consensus(symbol)
    if consensus:
        result.analyst_buy_pct     = _nan(consensus.get("analyst_buy_pct"))
        result.analyst_target_avg  = _nan(consensus.get("analyst_target_avg"))
        result.analyst_target_high = _nan(consensus.get("analyst_target_high"))
        result.analyst_total       = int(consensus.get("analyst_total") or 0)
        result.analyst             = consensus
    result.insider_trades = client.get_insider_trades(symbol, limit=3)
    result.quote = quote

    # 今日涨幅预估（用纯技术强度，不受市场数据加成影响，防止虚高）
    result.estimated_gain_pct, result.estimated_gain_raw_pct = _estimate_gain(
        direction=result.direction,
        strength=result.strength,    # 此时仍为技术评分
        atr_pct=atr_pct,
        pre_change_pct=result.pre_change_pct,
        volume_ratio=result.volume_ratio,
        bid_ask_ratio=result.bid_ask_ratio,
    )

    # ── 市场数据评分叠加（七大类别：量价/资金流/机构共识/估值/事件/内部人/空头）──
    if result.direction != "NEUTRAL":
        mkt_delta, mkt_reasons = _market_score(result)
        if mkt_delta != 0:
            result.strength = max(0, min(100, result.strength + mkt_delta))
            result.reasons.extend(mkt_reasons)
            logger.debug("%s 市场数据修正 %+d → 最终强度 %d", symbol, mkt_delta, result.strength)

    # 仓位建议（用最终强度计算，放在所有评分完成后）
    if result.direction == "BUY" and current_price > 0:
        position_budget = total_capital * max_position_pct * (result.strength / 100)
        result.position_shares = max(1, int(position_budget / current_price))
        # 展示金额必须是"这些股数实际要花多少钱"，不能直接显示预算——预算除以
        # 股价取整成股数后，真实花费经常跟预算差很多（股价较高、预算买不到
        # 几股时尤其明显，比如预算$3050但股价$1722，只够买1股，之前直接显示
        # $3050导致"建议仓位:1股(约$3050)"这种股数和金额对不上的误导性文案）
        result.position_usd = round(result.position_shares * current_price, 2)

    logger.info(
        "%s → %s (强度 %d，预估 %+.1f%%，当前价 %.2f)",
        symbol, result.direction, result.strength, result.estimated_gain_pct, current_price,
    )
    return result
