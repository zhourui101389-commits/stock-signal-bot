"""
AI 深度分析模块：用 Claude 对股票做多维综合研判，替代规则打分。
输入：yfinance 技术/基本面数据 + Finnhub 新闻/评级/财报
输出：结构化的深度分析报告（JSON）
"""
import json
import logging
import math
from datetime import datetime

import anthropic

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_SYSTEM_PROMPT = """你是一位资深美股分析师，专注于短期交易（3-7天）和波段操作（2-4周）。
你的分析风格：
- 直接给出明确判断，不说废话
- 技术面和基本面结合，但以技术面为主
- 特别关注：成交量异动、主力资金行为、催化剂时间窗口
- 用中文输出，语言简洁有力

你必须严格按照 JSON 格式输出，不要有任何 JSON 之外的内容。"""

_USER_PROMPT_TEMPLATE = """
分析股票：{symbol}（当前价 {price}，所属行业：{sector}）

## 技术面数据
- 日线趋势：{daily_trend}
- RSI(14)：{rsi:.1f}
- MACD：{macd_trend}
- 布林带位置：{bb_position}
- 成交量比率（今日/10日均量）：{volume_ratio:.2f}x
- 短期趋势（MA5/MA20）：{ma_short_trend}
- 中期趋势（MA20/MA50 周线）：{ma_mid_trend}
- ADX（趋势强度）：{adx:.1f}
- ROC20（近20日涨跌）：{roc20:+.1f}%
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

## 分析师评级近期变动
{rating_changes}

## 近3天重要新闻
{news}

## 宏观背景
{macro_context}

---
请以 JSON 格式输出以下分析（不要输出 JSON 以外任何内容）：

{{
  "direction": "看多|看空|中性",
  "confidence": "高|中|低",
  "horizon": "3-5天|1-2周|2-4周",
  "price_now": {price_now},
  "target_bull": <乐观目标价，数字>,
  "target_bear": <悲观目标价，数字>,
  "verdict": "<20字以内的核心判断>",
  "analysis": "<150-250字深度分析：技术形态→基本面支撑→催化剂→风险点，要具体>",
  "bull_case": "<60字，多方理由>",
  "bear_case": "<60字，空方风险>",
  "key_level_support": <关键支撑价，数字>,
  "key_level_resistance": <关键阻力价，数字>,
  "catalyst": "<近期催化剂，无则填null>",
  "action": "积极买入|谨慎买入|持有观望|减仓|回避",
  "stop_loss": <止损价，数字>
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


def _fmt_rating_changes(changes: list[dict]) -> str:
    if not changes:
        return "近期无变动"
    lines = []
    for c in changes[:3]:
        action = c.get("action", "")
        frm = c.get("from", "")
        to = c.get("to", "")
        company = c.get("company", "")
        date = c.get("date", "")
        emoji = "⬆️" if "upgrade" in action.lower() else "⬇️" if "downgrade" in action.lower() else "➡️"
        lines.append(f"  {emoji} {date} {company}: {frm}→{to}")
    return "\n".join(lines) if lines else "近期无变动"


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
    close  = last("close") or result.price
    bb_up  = last("bb_upper")
    bb_low = last("bb_lower")
    bb_mid = last("bb_mid")
    roc20  = last("roc20") or 0.0

    # 日线趋势判断
    if ma200 and close > ma200 * 1.02:
        daily_trend = f"多头（收盘价 {close:.2f} 在MA200上方 {((close/ma200-1)*100):+.1f}%）"
    elif ma200 and close < ma200 * 0.98:
        daily_trend = f"空头（收盘价 {close:.2f} 在MA200下方 {((close/ma200-1)*100):+.1f}%）"
    else:
        daily_trend = f"中性震荡（收盘价 {close:.2f} 接近MA200）"

    macd_trend = "金叉看多" if macd > signal else "死叉看空"
    if abs(macd - signal) < 0.01:
        macd_trend = "交叉临界"

    if bb_up and bb_low and bb_mid:
        pos = (close - bb_low) / (bb_up - bb_low) * 100 if (bb_up - bb_low) > 0 else 50
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


def run_ai_analysis(
    result,           # SignalResult
    finnhub,          # FinnhubClient（可为 None）
    anthropic_key: str,
    macro_context: str = "",
) -> dict:
    """
    对单只股票运行 AI 深度分析。
    返回结构化 dict，失败时返回 {}。
    """
    if not anthropic_key:
        logger.warning("ANTHROPIC_API_KEY 未配置，跳过 AI 分析")
        return {}

    symbol = result.symbol
    price  = result.current_price if not math.isnan(result.current_price) else result.close_price

    # ── 技术面 ────────────────────────────────────────
    tech = _extract_technical(result)

    # ── 基本面（yfinance 已有） ───────────────────────
    q = result.quote or {}
    volume_ratio  = q.get("volume_ratio", 1.0) or 1.0
    short_pct_val = result.short_data.get("short_pct") if result.short_data else None
    short_pct_str = _safe_pct(short_pct_val)

    analyst = result.analyst or {}
    analyst_buy  = analyst.get("buy_pct", 0) or 0
    analyst_cnt  = analyst.get("total",   0) or 0

    # ── Finnhub 数据 ──────────────────────────────────
    news, rating_changes, earnings_surprise = [], [], []
    pe_ttm = rev_growth = eps_growth = roe_val = beta_val = None
    pct_52w = 50.0

    if finnhub:
        try:
            news           = finnhub.get_company_news(symbol, days=3)
        except Exception as e:
            logger.debug("Finnhub news %s: %s", symbol, e)
        # upgrade-downgrade 在免费套餐返回 403，跳过
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

    # ── 组装 Prompt ───────────────────────────────────
    sector = q.get("sector", "未知行业") if q else "未知行业"
    prompt = _USER_PROMPT_TEMPLATE.format(
        symbol=symbol,
        price=f"{price:.2f}",
        sector=sector,
        daily_trend=tech["daily_trend"],
        rsi=tech["rsi"],
        macd_trend=tech["macd_trend"],
        bb_position=tech["bb_position"],
        volume_ratio=volume_ratio,
        ma_short_trend=tech["ma_short_trend"],
        ma_mid_trend=tech["ma_mid_trend"],
        adx=tech["adx"],
        roc20=tech["roc20"],
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
        rating_changes=_fmt_rating_changes(rating_changes),
        news=_fmt_news(news),
        macro_context=macro_context or "当前无特别宏观事件",
        price_now=price,
    )

    # ── 调用 Claude（最多重试2次）────────────────────
    client = anthropic.Anthropic(api_key=anthropic_key)
    for attempt in range(3):
        try:
            message = client.messages.create(
                model=_MODEL,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()

            # 提取 JSON：处理 markdown 代码块、前后多余文字
            if "```" in raw:
                parts = raw.split("```")
                for p in parts:
                    p = p.strip()
                    if p.startswith("json"):
                        p = p[4:].strip()
                    if p.startswith("{"):
                        raw = p
                        break
            # 找到第一个 { 到最后一个 }
            start = raw.find("{")
            end   = raw.rfind("}") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]

            analysis = json.loads(raw)
            analysis["symbol"] = symbol
            logger.info("AI 分析完成 %s: %s（置信度:%s）",
                        symbol, analysis.get("direction"), analysis.get("confidence"))
            return analysis

        except json.JSONDecodeError as e:
            logger.warning("AI JSON 解析失败 %s 第%d次: %s", symbol, attempt+1, e)
            if attempt == 2:
                logger.error("AI 分析放弃 %s，原文片段: %s", symbol, raw[:300])
                return {}
        except Exception as e:
            logger.error("AI 分析失败（%s）: %s", symbol, e)
            return {}
    return {}
