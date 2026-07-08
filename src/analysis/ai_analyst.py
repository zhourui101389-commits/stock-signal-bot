"""
AI 综合研判模块：接收技术系统信号 + 基本面数据，输出统一操作判断。
AI 作为裁判，确认或推翻技术信号，给出一个确定性结论。
"""
import json
import logging
import math
from datetime import datetime

import anthropic

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = """你是一位资深美股量化分析师，负责综合量化技术信号和基本面数据，给出一个最终统一的操作判断。

你的工作规则：
- 技术系统已给出初步信号（方向+强度+理由），你需要评估并确认或推翻它
- 技术和基本面一致 → 提高置信度，给出明确建议
- 两者矛盾 → 判断哪个更可信，给出最终方向并说明推翻理由
- 只输出一个操作建议，不能两边倒或模糊
- 用中文，语言简洁有力
- 严格按 JSON 格式输出，不含任何 JSON 以外内容

止盈止损必须基于 ATR（真实日均波幅），禁止使用固定百分比：
- 止损 = 入场价 - 1.5×ATR（低波动大盘股）到 entry - 2×ATR（高波动股/杠杆ETF如SOXL）
- 目标价必须满足盈亏比 ≥ 2:1，即 (target - entry) ≥ 2 × (entry - stop_loss)
- 优先对齐近期技术阻力位或支撑位，而非机械套公式
- 中性方向（不入场）时 stop_loss 和 target_price 填 null

持仓期判断：
- "3-5天"适合动量短线，"1-2周"适合波段，"2-4周"适合趋势
- 评估不绑定固定日期，止盈或止损触发即退出；持满窗口未触达则在最后一个交易日平仓"""

_USER_PROMPT_TEMPLATE = """
分析股票：{symbol}（当前价 {price}，所属行业：{sector}）

## 技术系统初步信号
- 方向: {tech_direction}（BUY=看多/SELL=看空/NEUTRAL=中性观望）
- 强度: {tech_strength}/100
- 信号理由:
{tech_reasons}

## 技术面指标
- 日线趋势：{daily_trend}
- RSI(14)：{rsi:.1f}
- MACD：{macd_trend}
- 布林带位置：{bb_position}
- 成交量比率（今日/10日均量）：{volume_ratio:.2f}x
- 短期趋势（MA5/MA20）：{ma_short_trend}
- 中期趋势（MA20/MA50 周线）：{ma_mid_trend}
- ADX（趋势强度）：{adx:.1f}
- ROC20（近20日涨跌）：{roc20:+.1f}%
- ATR(14)日均波幅：{atr_pct:.1f}%（约 ${atr_dollar:.2f}/日）
  → 止损参考：1.5×ATR=${atr_1_5x:.2f} / 2×ATR=${atr_2x:.2f}（从入场价扣减）
  → 最低目标价（2:1盈亏比）= 入场价 + 2×止损距离
- 做空比例：{short_pct}

## 基本面数据
- 分析师共识买入比例：{analyst_buy_pct}%（共 {analyst_count} 人）
- 市盈率(TTM)：{pe_ttm}
- EPS同比增长：{eps_growth}
- 营收同比增长：{revenue_growth}
- ROE：{roe}
- Beta：{beta}
- 52周位置：{pct_52w:.0f}%（0%=52周低点，100%=52周高点）

## 近期财报超预期记录
{earnings_surprise}

## 财报预警
{earnings_warning}

## 近3天重要新闻
{news}

## 历史预测记录（近期）
{history}

## 宏观背景
{macro_context}

---
请综合技术信号和基本面，给出统一判断。
如有历史预测记录，请识别判断规律：
- 有效出局准确率（止盈/止损/持满T+5）是主要考核指标，反映市场驱动的真实盈亏，不是固定日期
- T+0当日仅供参考，含入场前行情，可能虚高
- "止盈后还涨X%"说明目标价设偏低，下次应上调止盈位；"止损后反弹X%"说明止损设偏紧
- 卖出质量分低（<40%）说明退出时机差，需改善止盈止损设置
- 连续出局亏损说明该股信号在当前市场环境中失效
财报预警期内技术信号可靠性大幅下降，应降低置信度或转为观望。
以 JSON 格式输出（不要输出 JSON 以外任何内容）：

{{
  "tech_confirmed": true或false（是否确认技术系统的方向判断）,
  "override_reason": "<推翻理由，若tech_confirmed=true则填null>",
  "final_direction": "看多|看空|中性",
  "conviction": "高|中|低",
  "horizon": "3-5天|1-2周|2-4周",
  "verdict": "<20字以内核心判断，点明最重要的一个逻辑>",
  "analysis": "<150-250字综合分析：技术信号可信度→基本面支撑→催化剂→风险点，要具体>",
  "bull_case": "<60字，做多理由>",
  "bear_case": "<60字，做空风险>",
  "action": "积极买入|谨慎买入|持有观望|减仓|回避",
  "target_price": <目标价（具体数字，盈亏比≥2:1；中性方向填null）>,
  "stop_loss": <止损价（具体数字，基于ATR；中性方向填null）>,
  "key_level_support": <关键支撑价，数字>,
  "key_level_resistance": <关键阻力价，数字>,
  "catalyst": "<近期催化剂，无则填null>"
}}
"""


def _safe_pct(val, default="未知") -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return default
    return f"{val:.1f}%"


def _fmt_earnings(earnings: list[dict]) -> str:
    if not earnings:
        return "暂无数据"
    lines = []
    for e in earnings[:4]:
        period = e.get("period", "")
        surprise = e.get("surprise")
        if surprise is None:
            lines.append(f"  {period}: 无数据")
        elif surprise > 0:
            lines.append(f"  {period}: ✅ 超预期 {surprise:+.1f}%")
        else:
            lines.append(f"  {period}: ❌ 不及预期 {surprise:+.1f}%")
    return "\n".join(lines) if lines else "暂无数据"


def _fmt_news(news: list[dict]) -> str:
    if not news:
        return "暂无重要新闻"
    lines = []
    for n in news[:5]:
        headline = n.get("headline", "")[:80]
        source = n.get("source", "")
        time_ = n.get("time", "")
        lines.append(f"  [{time_}] {headline}（{source}）")
    return "\n".join(lines)


def _extract_technical(result) -> dict:
    """从 SignalResult 提取技术面指标。"""
    d = result.daily_df
    w = result.weekly_df

    def last(col):
        try:
            v = d[col].dropna().iloc[-1]
            return float(v) if not math.isnan(float(v)) else None
        except Exception:
            return None

    rsi    = last("rsi") or 50.0
    macd   = last("macd") or 0.0
    signal = last("macd_signal") or 0.0
    adx    = last("adx") or 20.0
    ma5    = last("ma5")
    ma20   = last("ma20")
    ma50   = last("ma50")
    ma200  = last("ma200")
    close  = last("close") or result.current_price
    bb_up  = last("bb_upper")
    bb_low = last("bb_lower")
    bb_mid = last("bb_mid")
    roc20  = last("roc20") or 0.0

    if ma200 and close > ma200 * 1.02:
        daily_trend = f"多头（收盘价 {close:.2f} 在MA200上方 {((close/ma200-1)*100):+.1f}%）"
    elif ma200 and close < ma200 * 0.98:
        daily_trend = f"空头（收盘价 {close:.2f} 在MA200下方 {((close/ma200-1)*100):+.1f}%）"
    else:
        daily_trend = f"中性震荡（收盘价 {close:.2f} 接近MA200）"

    macd_trend = "金叉看多" if macd > signal else "死叉看空"
    if abs(macd - signal) < 0.01:
        macd_trend = "交叉临界"

    if bb_up and bb_low and (bb_up - bb_low) > 0:
        pos = (close - bb_low) / (bb_up - bb_low) * 100
        if pos > 80:
            bb_position = f"上轨附近（{pos:.0f}%），可能超买"
        elif pos < 20:
            bb_position = f"下轨附近（{pos:.0f}%），可能超卖"
        else:
            bb_position = f"布林带中段（{pos:.0f}%），方向不明"
    else:
        bb_position = "数据不足"

    if ma5 and ma20:
        ma_short = f"{'MA5＞MA20多头' if ma5 > ma20 else 'MA5＜MA20空头'}（差距{abs(ma5-ma20)/ma20*100:.1f}%）"
    else:
        ma_short = "数据不足"

    try:
        wma20 = float(w["ma20"].dropna().iloc[-1])
        wma50 = float(w["ma50"].dropna().iloc[-1])
        ma_mid = f"{'周线多头' if wma20 > wma50 else '周线空头'}（MA20={wma20:.2f}/MA50={wma50:.2f}）"
    except Exception:
        ma_mid = "数据不足"

    return {
        "rsi": rsi, "adx": adx, "roc20": roc20,
        "daily_trend": daily_trend, "macd_trend": macd_trend,
        "bb_position": bb_position, "ma_short_trend": ma_short,
        "ma_mid_trend": ma_mid, "close": close,
    }


def _fmt_history(history: list[dict]) -> str:
    if not history:
        return "无历史预测记录"

    def _eff_correct(h: dict):
        """
        准确率以市场驱动退出结果为准（优先级：有效退出 > T+3 > T+0）。
        - exit_tracked=True：用 effective_exit_pct 对比方向（止盈/止损/持满T+5）
        - 否则用 T+3（中期代理）
        - 最后回退 T+0（仅参考，含入场前行情）
        """
        if h.get("exit_tracked"):
            exit_pct  = h.get("effective_exit_pct")
            final_dir = h.get("final_direction", "中性")
            if exit_pct is not None:
                if final_dir == "看多":
                    return exit_pct > 0
                elif final_dir == "看空":
                    return exit_pct < 0
        t3 = h.get("t3_correct")
        return t3 if t3 is not None else h.get("correct")

    lines = []
    for h in history[-5:]:
        actual  = h.get("actual_pct")
        correct = h.get("correct")    # T+0
        t3_pct  = h.get("t3_pct")
        t5_pct  = h.get("t5_pct")
        t3_cor  = h.get("t3_correct")
        t5_cor  = h.get("t5_correct")

        # 主图标：有效退出（市场驱动）> T+3 > T+0
        eff = _eff_correct(h)
        exit_pct_v   = h.get("effective_exit_pct")
        exit_reason  = h.get("effective_exit_reason")
        exit_tracked = h.get("exit_tracked")

        if exit_tracked and exit_pct_v is not None:
            main_icon  = "✅" if eff else "❌"
            if (exit_reason or "").startswith("held_to_t"):
                win = exit_reason.replace("held_to_t", "")
                reason_label = f"持满T+{win}"
            else:
                reason_label = {"hit_target": "止盈", "hit_stop": "止损"}.get(exit_reason or "", "退出")
            main_label = f"{reason_label}{exit_pct_v:+.2f}%"
        elif t3_cor is not None:
            main_icon  = "✅" if t3_cor else "❌"
            main_label = f"T+3波段{t3_pct:+.2f}%" if t3_pct is not None else "T+3"
        else:
            main_icon  = "✅" if correct is True else ("❌" if correct is False else "⚪")
            main_label = f"T+0当日{actual:+.2f}%" if actual is not None else "T+0待复盘"

        conviction  = h.get("conviction", "")
        conv_str    = f"[{conviction}]" if conviction else ""
        verdict     = h.get("verdict", "")
        verdict_str = f" 「{verdict}」" if verdict else ""

        line = (
            f"  {main_icon} {h.get('scan_date','?')}: "
            f"{h.get('final_direction','?')}{conv_str}{verdict_str} → {main_label}"
        )

        # 附加对比数据
        extra = []
        if t3_cor is not None and actual is not None:
            # 显示 T+0 作为对比
            t0_icon = "✅" if correct is True else ("❌" if correct is False else "⚪")
            extra.append(f"T+0{t0_icon}{actual:+.2f}%")
        if t5_pct is not None:
            t5_icon = "✅" if t5_cor is True else ("❌" if t5_cor is False else "⚪")
            extra.append(f"T+5{t5_icon}{t5_pct:+.2f}%")
        if extra:
            line += "  " + " ".join(extra)

        # 当日与最终结果方向相反时标注
        if eff is not None and correct is not None and eff != correct:
            line += "  ⚠️当日正最终反" if correct and not eff else "  🔄当日跌最终涨"

        # 退出细节（止盈/止损/持满）
        if exit_tracked:
            eq         = h.get("exit_quality")
            peak_pct   = h.get("holding_peak_pct")
            stop_hit_d = h.get("stop_hit_day")
            tgt_hit_d  = h.get("target_hit_day")
            exit_parts = []
            if exit_reason == "hit_target":
                exit_parts.append(f"止盈T+{tgt_hit_d}:{exit_pct_v:+.1f}%")
                missed = (peak_pct or 0) - (exit_pct_v or 0)
                if missed > 0.5:
                    exit_parts.append(f"后续+{missed:.1f}%")
            elif exit_reason == "hit_stop":
                exit_parts.append(f"止损T+{stop_hit_d}:{exit_pct_v:+.1f}%")
                recovery = (peak_pct or 0) - (exit_pct_v or 0)
                if recovery > 0.5:
                    exit_parts.append(f"反弹{recovery:.1f}%（止损过紧？）")
            elif (exit_reason or "").startswith("held_to_t"):
                win = exit_reason.replace("held_to_t", "")
                exit_parts.append(f"持满T+{win}:{exit_pct_v:+.1f}%")
            if eq is not None:
                exit_parts.append(f"卖质{int(eq*100)}%")
            if exit_parts:
                line += "  退出:" + "/".join(exit_parts)

        lines.append(line)

    # 有效退出准确率（市场驱动：止盈/止损/持满T+5，主要考核）
    eff_counted = [h for h in history[-10:] if _eff_correct(h) is not None]
    if len(eff_counted) >= 2:
        n_eff = sum(1 for h in eff_counted if _eff_correct(h))
        has_exit = sum(1 for h in eff_counted if h.get("exit_tracked"))
        src = f"其中{has_exit}笔已触达止盈/止损" if has_exit else "T+3代理"
        lines.append(f"  ⭐ 有效出局准确率: {n_eff}/{len(eff_counted)}（{src}，主要考核）")

    # T+0 当日准确率（仅参考，含入场前行情，可能虚高）
    counted = [h for h in history[-10:] if h.get("correct") is not None]
    if len(counted) >= 3:
        n_right = sum(1 for h in counted if h.get("correct"))
        lines.append(f"  T+0当日准确率: {n_right}/{len(counted)}（仅参考，含入场前行情）")

    # 置信度校准：用 T+3 优先
    high_conv = [h for h in history[-15:]
                 if h.get("conviction") == "高" and _eff_correct(h) is not None]
    if len(high_conv) >= 3:
        n_hc = sum(1 for h in high_conv if _eff_correct(h))
        rate  = n_hc / len(high_conv) * 100
        note  = "✅ 高置信预测偏准" if rate >= 60 else "⚠️ 高置信预测不可靠"
        lines.append(f"  高置信度准确率: {n_hc}/{len(high_conv)}（{rate:.0f}%） {note}")

    # 连续错误提示（有效退出优先）
    recent = [h for h in history[-3:] if _eff_correct(h) is not None]
    if len(recent) >= 3 and all(_eff_correct(h) is False for h in recent):
        lines.append("  ⚠️ 近3次有效出局均亏损，请重新评估该股信号有效性")

    # 卖出质量汇总
    eq_list = [h.get("exit_quality") for h in history if h.get("exit_quality") is not None]
    if len(eq_list) >= 2:
        avg_eq  = sum(eq_list) / len(eq_list)
        eq_icon = "✅" if avg_eq >= 0.7 else ("⚠️" if avg_eq >= 0.4 else "❌")
        lines.append(f"  {eq_icon} 历史平均卖出质量: {avg_eq*100:.0f}%（越高越接近最优出局时机）")

    return "\n".join(lines)


def run_ai_analysis(
    result,
    finnhub,
    anthropic_key: str,
    macro_context: str = "",
    symbol_history: list[dict] = None,
) -> dict:
    """
    综合技术信号和基本面，输出统一操作判断。
    返回结构化 dict，失败时返回 {}。
    """
    if not anthropic_key:
        logger.warning("ANTHROPIC_API_KEY 未配置，跳过 AI 分析")
        return {}

    symbol = result.symbol
    price  = result.current_price if not math.isnan(result.current_price) else result.close_price

    tech = _extract_technical(result)

    q = result.quote or {}
    volume_ratio  = q.get("volume_ratio", 1.0) or 1.0
    short_pct_val = result.short_data.get("short_pct") if result.short_data else None
    short_pct_str = _safe_pct(short_pct_val)

    analyst = result.analyst or {}
    analyst_buy  = analyst.get("buy_pct", 0) or 0
    analyst_cnt  = analyst.get("total",   0) or 0

    news, earnings_surprise = [], []
    pe_ttm = rev_growth = eps_growth = roe_val = beta_val = None
    pct_52w = 50.0

    if finnhub:
        try:
            news = finnhub.get_company_news(symbol, days=3)
        except Exception as e:
            logger.debug("Finnhub news %s: %s", symbol, e)
        try:
            earnings_surprise = finnhub.get_earnings_surprise(symbol)
        except Exception as e:
            logger.debug("Finnhub earnings %s: %s", symbol, e)
        try:
            fin = finnhub.get_basic_financials(symbol)
            pe_ttm      = fin.get("pe_ttm")
            eps_growth  = fin.get("eps_growth_ttm")
            rev_growth  = fin.get("revenue_growth_ttm")
            roe_val     = fin.get("roe")
            beta_val    = fin.get("beta")
            hi52        = fin.get("52w_high")
            lo52        = fin.get("52w_low")
            if hi52 and lo52 and hi52 > lo52:
                pct_52w = (price - lo52) / (hi52 - lo52) * 100
        except Exception as e:
            logger.debug("Finnhub financials %s: %s", symbol, e)

    # ATR 计算（用于止损指引）
    atr_pct_val = result.atr_pct if not math.isnan(result.atr_pct) else 3.0
    atr_dollar  = price * atr_pct_val / 100
    atr_1_5x    = atr_dollar * 1.5
    atr_2x      = atr_dollar * 2.0

    # 技术信号理由格式化
    tech_direction = result.direction  # BUY / SELL / NEUTRAL
    tech_strength  = result.strength
    tech_reasons   = "\n".join(f"  • {r}" for r in result.reasons) if result.reasons else "  • 无"

    # 财报预警
    next_earnings = getattr(result, "next_earnings_date", "") or ""
    earnings_warning = "暂无近期财报信息"
    if next_earnings:
        try:
            from datetime import date as _date
            ed = _date.fromisoformat(next_earnings[:10])
            days_left = (ed - _date.today()).days
            if days_left < 0:
                earnings_warning = f"财报已于 {next_earnings} 发布"
            elif days_left <= 3:
                earnings_warning = f"⚠️ {days_left} 天后财报（{next_earnings}）——技术信号极不可靠，强烈建议中性观望"
            elif days_left <= 7:
                earnings_warning = f"⚠️ {days_left} 天后财报（{next_earnings}）——置信度应降级，避免重仓"
            else:
                earnings_warning = f"下次财报 {next_earnings}（{days_left} 天后），暂无直接影响"
        except Exception:
            earnings_warning = f"下次财报：{next_earnings}"

    sector = q.get("sector", "未知行业") if q else "未知行业"
    prompt = _USER_PROMPT_TEMPLATE.format(
        symbol=symbol,
        price=f"{price:.2f}",
        sector=sector,
        tech_direction=tech_direction,
        tech_strength=tech_strength,
        tech_reasons=tech_reasons,
        daily_trend=tech["daily_trend"],
        rsi=tech["rsi"],
        macd_trend=tech["macd_trend"],
        bb_position=tech["bb_position"],
        volume_ratio=volume_ratio,
        ma_short_trend=tech["ma_short_trend"],
        ma_mid_trend=tech["ma_mid_trend"],
        adx=tech["adx"],
        roc20=tech["roc20"],
        atr_pct=atr_pct_val,
        atr_dollar=atr_dollar,
        atr_1_5x=atr_1_5x,
        atr_2x=atr_2x,
        short_pct=short_pct_str,
        analyst_buy_pct=f"{analyst_buy:.0f}",
        analyst_count=analyst_cnt,
        pe_ttm=f"{pe_ttm:.1f}" if pe_ttm else "未知",
        eps_growth=_safe_pct(eps_growth * 100 if eps_growth else None),
        revenue_growth=_safe_pct(rev_growth * 100 if rev_growth else None),
        roe=_safe_pct(roe_val),
        beta=f"{beta_val:.2f}" if beta_val else "未知",
        pct_52w=pct_52w,
        earnings_surprise=_fmt_earnings(earnings_surprise),
        earnings_warning=earnings_warning,
        news=_fmt_news(news),
        history=_fmt_history(symbol_history or []),
        macro_context=macro_context or "当前无特别宏观事件",
    )

    client = anthropic.Anthropic(api_key=anthropic_key)
    raw = ""
    for attempt in range(3):
        try:
            message = client.messages.create(
                model=_MODEL,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()

            if "```" in raw:
                for p in raw.split("```"):
                    p = p.strip()
                    if p.startswith("json"):
                        p = p[4:].strip()
                    if p.startswith("{"):
                        raw = p
                        break
            start = raw.find("{")
            end   = raw.rfind("}") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]

            analysis = json.loads(raw)
            analysis["symbol"] = symbol
            confirmed = analysis.get("tech_confirmed", True)
            logger.info("AI综合研判 %s: %s（%s技术信号，置信度:%s）",
                        symbol, analysis.get("final_direction"),
                        "确认" if confirmed else "推翻",
                        analysis.get("conviction"))
            return analysis

        except json.JSONDecodeError as e:
            logger.warning("AI JSON 解析失败 %s 第%d次: %s", symbol, attempt + 1, e)
            if attempt == 2:
                logger.error("AI 分析放弃 %s，原文片段: %s", symbol, raw[:300])
                return {}
        except Exception as e:
            logger.error("AI 分析失败（%s）: %s", symbol, e)
            return {}
    return {}
