"""
美国宏观经济日历
数据来源：Finnhub.io（免费 API key，注册地址 https://finnhub.io）
配置：config/settings.yaml → economic_calendar.finnhub_api_key
"""
import logging
import math
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)
_CST = timezone(timedelta(hours=8))
_ET  = timezone(timedelta(hours=-4))   # EDT（夏令时）

# ── 事件中文名映射 ─────────────────────────────────────────────────────────────

_EVENT_CN: dict[str, str] = {
    # 就业
    "Nonfarm Payrolls":                   "非农就业人数",
    "Unemployment Rate":                  "失业率",
    "Average Hourly Earnings MoM":        "平均时薪(月率)",
    "Average Hourly Earnings YoY":        "平均时薪(年率)",
    "JOLTs Job Openings":                 "JOLTS职位空缺",
    "ADP Nonfarm Payrolls":               "ADP非农就业",
    "Initial Jobless Claims":             "初请失业金(周)",
    "Continuing Jobless Claims":          "续请失业金",
    # 通胀
    "CPI MoM":                            "CPI 月率",
    "CPI YoY":                            "CPI 年率",
    "Core CPI MoM":                       "核心CPI 月率",
    "Core CPI YoY":                       "核心CPI 年率",
    "PPI MoM":                            "PPI 月率",
    "PPI YoY":                            "PPI 年率",
    "Core PCE Price Index MoM":           "核心PCE 月率",
    "PCE Price Index MoM":                "PCE 月率",
    # 美联储
    "Fed Interest Rate Decision":         "🏦 美联储利率决议",
    "FOMC Meeting Minutes":               "🏦 FOMC 会议纪要",
    "Federal Reserve Press Conference":   "🏦 鲍威尔新闻发布会",
    # 增长
    "GDP QoQ":                            "GDP 季率",
    "GDP Growth Rate QoQ Adv":            "GDP 初值(季率)",
    "GDP Growth Rate QoQ 2nd Est":        "GDP 二次修正(季率)",
    "GDP Growth Rate QoQ Final":          "GDP 终值(季率)",
    "Retail Sales MoM":                   "零售销售月率",
    "Core Retail Sales MoM":              "核心零售销售月率",
    "Durable Goods Orders MoM":           "耐用品订单月率",
    # PMI
    "ISM Manufacturing PMI":              "ISM制造业PMI",
    "ISM Services PMI":                   "ISM非制造业PMI",
    "S&P Global Manufacturing PMI":       "Markit制造业PMI",
    "S&P Global Services PMI":            "Markit服务业PMI",
    # 消费者
    "CB Consumer Confidence":             "CB消费者信心",
    "Michigan Consumer Sentiment":        "密歇根消费者信心",
    "Michigan Consumer Sentiment Final":  "密歇根消费者信心(终值)",
    # 地产
    "New Home Sales":                     "新屋销售",
    "Existing Home Sales":                "成屋销售",
    "Housing Starts":                     "新屋开工",
    "Building Permits":                   "建筑许可",
    # 其他
    "Trade Balance":                      "贸易差额",
    "Crude Oil Inventories":              "原油库存(EIA)",
    "Natural Gas Storage":                "天然气库存",
    "Treasury Auctions":                  "国债拍卖",
}

# 影响方向：True = 数值越高越对股市有利，False = 越低越好，None = 特殊处理
_DIRECTION: dict[str, bool | None] = {
    "Nonfarm Payrolls":              None,   # 高=就业强 but 可能偏鹰
    "ADP Nonfarm Payrolls":         None,
    "JOLTs Job Openings":           None,
    "Unemployment Rate":            False,   # 低失业率好
    "Initial Jobless Claims":       False,   # 低申请好
    "Continuing Jobless Claims":    False,
    "CPI MoM":                      False,   # 通胀低 = 利好
    "CPI YoY":                      False,
    "Core CPI MoM":                 False,
    "Core CPI YoY":                 False,
    "PPI MoM":                      False,
    "PPI YoY":                      False,
    "Core PCE Price Index MoM":     False,
    "PCE Price Index MoM":          False,
    "GDP QoQ":                      True,
    "GDP Growth Rate QoQ Adv":      True,
    "Retail Sales MoM":             True,
    "Core Retail Sales MoM":        True,
    "Durable Goods Orders MoM":     True,
    "ISM Manufacturing PMI":        True,
    "ISM Services PMI":             True,
    "S&P Global Manufacturing PMI": True,
    "S&P Global Services PMI":      True,
    "CB Consumer Confidence":       True,
    "Michigan Consumer Sentiment":  True,
    "New Home Sales":               True,
    "Existing Home Sales":          True,
    "Housing Starts":               True,
    "Building Permits":             True,
    "Trade Balance":                True,    # 贸易差额越大越好
}


def _cn_name(event: str) -> str:
    return _EVENT_CN.get(event, event[:28])


def _impact_icon(impact: str) -> str:
    i = (impact or "").lower()
    return "🔴" if i == "high" else ("🟡" if i == "medium" else "⚪")


def _fmt_val(v, unit: str) -> str:
    """格式化数值，自动添加单位。"""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    try:
        f = float(v)
        u = (unit or "").strip()
        if u == "%":
            return f"{f:+.2f}%"
        if u in ("K", "M", "B"):
            return f"{f:.1f}{u}"
        return f"{f:+.2f}" if abs(f) < 1000 else f"{f:.0f}"
    except (TypeError, ValueError):
        return str(v)


def _forecast_comment(event: str, est, prev, unit: str) -> str:
    """预估 vs 前值，生成预判文字（事件尚未公布时）。"""
    try:
        e, p = float(est), float(prev)
    except (TypeError, ValueError):
        return ""

    diff = e - p
    direction = _DIRECTION.get(event)

    # PMI 特殊：以 50 为荣枯线
    if "PMI" in event:
        if e >= 50 > p:
            return f"预估 {e:.1f}，由收缩转扩张 ✅ 积极信号"
        if e < 50 <= p:
            return f"预估 {e:.1f}，由扩张转收缩 ⚠️ 注意风险"
        arrow = "改善" if e > p else "走弱"
        zone  = "扩张" if e >= 50 else "收缩"
        return f"预估 {e:.1f}，{arrow}但仍在{zone}区"

    # 就业（非农/ADP/JOLTS）—— 两难：强就业 = 降息远
    if direction is None:
        if "Claims" in event:          # 初请、续请：低 = 好
            if diff < -5:
                return "申请减少 ✅ 就业市场改善"
            if diff > 5:
                return "申请增多 ⚠️ 就业走弱信号"
            return "与前值持平，影响有限"
        # NFP / ADP / JOLTS
        if diff > 30:
            return "强劲就业预期 ⚠️ 可能推迟降息，短期偏空"
        if diff < -30:
            return "就业预期放缓 → 降息预期升温，关注实际数据"
        return "就业数据预期平稳"

    # 通胀指标（低 = 好）
    if direction is False:
        if diff < -0.1:
            return "通胀预期降温 ✅ 有助于降息预期升温，利好股市"
        if diff > 0.1:
            return "通胀预期回升 ⚠️ 可能推迟降息，偏空压力"
        return "通胀预期平稳，市场影响有限"

    # 正向指标（高 = 好）
    if direction is True:
        if diff > 0:
            return "预期好转 ✅ 经济韧性支撑"
        if diff < 0:
            return "预期走弱 ⚠️ 关注需求侧压力"
        return "预期持平，影响有限"

    return ""


def _actual_comment(event: str, actual, est, prev, unit: str) -> str:
    """已公布：比较实际 vs 预估。"""
    try:
        a = float(actual)
        e = float(est) if est is not None else None
    except (TypeError, ValueError):
        return ""

    if e is None:
        return ""

    beat = a > e
    miss = a < e
    direction = _DIRECTION.get(event)

    # PMI
    if "PMI" in event:
        tag = "超预期" if beat else "低于预期"
        zone = "扩张区间 ✅" if a >= 50 else "收缩区间 ⚠️"
        return f"实际 {a:.1f}，{tag}，处于{zone}"

    # Claims：低 = 好
    if "Claims" in event:
        if miss:   return f"低于预估 ✅ 就业市场强劲"
        if beat:   return f"高于预估 ⚠️ 申请增多，就业偏弱"
        return "符合预估"

    if direction is False:   # 通胀：低 = 好
        if miss:   return f"低于预估 ✅ 通胀降温超预期，利好降息预期"
        if beat:   return f"高于预估 ⚠️ 通胀超预期，降息预期受压"
        return "符合预估，市场影响中性"

    if direction is None:    # NFP / ADP
        if beat:   return f"超预期就业强劲 ⚠️ 降息预期降低，短期偏空"
        if miss:   return f"低于预估，就业偏弱 → 降息预期升温"
        return "符合预期"

    if direction is True:    # 正向指标
        if beat:   return f"超预期 ✅ 经济强于预期，市场利好"
        if miss:   return f"低于预估 ⚠️ 不及预期，关注后续数据"
        return "符合预估"

    return ""


# ── 公开接口 ───────────────────────────────────────────────────────────────────

def get_us_events(
    api_key: str,
    days_before: int = 1,
    days_after: int = 3,
    min_impact: str = "medium",
) -> list[dict]:
    """
    拉取美国宏观经济事件（高+中影响力）。
    返回按 ET 时间排序的列表，每项含 _cn_name / _comment 字段。
    """
    if not api_key:
        logger.warning("未配置 Finnhub API key，跳过经济日历")
        return []

    now   = datetime.now(_CST)
    start = (now - timedelta(days=days_before)).strftime("%Y-%m-%d")
    end   = (now + timedelta(days=days_after)).strftime("%Y-%m-%d")

    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"from": start, "to": end, "token": api_key},
            timeout=8,
        )
        resp.raise_for_status()
        raw = resp.json().get("economicCalendar", [])
    except Exception as exc:
        logger.error("拉取 Finnhub 经济日历失败: %s", exc)
        return []

    levels = {"high", "medium"} if min_impact == "medium" else {"high"}
    result = []
    for ev in raw:
        if ev.get("country", "").upper() != "US":
            continue
        impact = (ev.get("impact") or "").lower()
        if impact not in levels:
            continue

        event   = ev.get("event", "")
        actual  = ev.get("actual")
        est     = ev.get("estimate")
        prev    = ev.get("prev")
        unit    = ev.get("unit", "")

        # 预判文字
        if actual is not None:
            comment = _actual_comment(event, actual, est, prev, unit)
        elif est is not None and prev is not None:
            comment = _forecast_comment(event, est, prev, unit)
        else:
            comment = ""

        # 特殊处理：FOMC / 美联储
        if any(k in event for k in ("Fed Interest Rate", "FOMC", "Federal Reserve")):
            comment = comment or "关注利率决议及措辞，重大波动风险"

        result.append({
            **ev,
            "cn_name": _cn_name(event),
            "comment": comment,
        })

    result.sort(key=lambda x: x.get("time", ""))
    return result
