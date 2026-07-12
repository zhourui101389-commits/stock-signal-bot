"""
Finnhub 数据客户端：新闻情绪、分析师评级变动、内部人交易。
免费 API，60次/分钟限制。
"""
import time
import logging
import requests
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_BASE = "https://finnhub.io/api/v1"


class FinnhubClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session = requests.Session()
        self._session.params = {"token": api_key}

    def _get(self, path: str, params: dict = None, retries: int = 2) -> dict | list:
        url = _BASE + path
        for attempt in range(retries + 1):
            try:
                r = self._session.get(url, params=params or {}, timeout=10)
                if r.status_code == 429:
                    time.sleep(2)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                if attempt == retries:
                    logger.warning("Finnhub %s 失败: %s", path, e)
                    return {}
                time.sleep(1)
        return {}

    def get_company_news(self, symbol: str, days: int = 3) -> list[dict]:
        """最近 N 天的公司新闻，返回列表，每条含 headline/summary/sentiment。"""
        now = datetime.now(timezone.utc)
        date_from = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        date_to = now.strftime("%Y-%m-%d")
        data = self._get("/company-news", {"symbol": symbol, "from": date_from, "to": date_to})
        if not isinstance(data, list):
            return []
        # 只取最近 10 条，按时间倒序
        items = sorted(data, key=lambda x: x.get("datetime", 0), reverse=True)[:10]
        result = []
        for item in items:
            result.append({
                "headline": item.get("headline", ""),
                "summary":  item.get("summary", ""),
                "source":   item.get("source", ""),
                "url":      item.get("url", ""),
                "time":     datetime.fromtimestamp(item.get("datetime", 0), tz=timezone.utc)
                                    .strftime("%m-%d %H:%M"),
            })
        return result

    # 供应链/地缘政治关键词：突发新闻密集期（如地缘冲突）里，纯"最新8条"
    # 排序会把还在发酵、真正影响个股基本面的供应链新闻挤出窗口——实测过：
    # 中国暂停氦气出口(影响半导体制造，07-10发布)在Iran冲突刷屏后掉到
    # 全量100条里的第35位，"最新8条"完全看不到。命中关键词的新闻即使
    # 不在最新8条内也强制带上，不受排名影响
    _MACRO_KEYWORDS = (
        "helium", "export ban", "export control", "export curb", "tariff",
        "sanctions", "hormuz", "taiwan strait", "chip export", "chip ban",
        "supply chain", "semiconductor export", "rare earth", "rare-earth",
    )

    def get_market_news(self, category: str = "general") -> list[dict]:
        """
        大盘宏观新闻（category: general / forex / crypto / merger）。
        最新8条 + 命中供应链/地缘关键词的最多5条（哪怕排名靠后），按时间倒序合并。
        """
        data = self._get("/news", {"category": category})
        if not isinstance(data, list):
            return []
        items_sorted = sorted(data, key=lambda x: x.get("datetime", 0), reverse=True)
        recent = items_sorted[:8]
        # 按关键词分槽而非按时间取前5：单一话题刷屏时（如中东冲突里"hormuz"
        # 反复出现）会占满所有槽位，把只出现一次但同样重要的不同话题
        # （如"helium"）挤掉——每个关键词最多贡献1条（取该关键词下最新的），
        # 保证话题多样性而不是话题热度
        keyword_hits = []
        seen_keywords = set()
        for i in items_sorted[8:]:
            headline_lower = i.get("headline", "").lower()
            for kw in self._MACRO_KEYWORDS:
                if kw in headline_lower and kw not in seen_keywords:
                    seen_keywords.add(kw)
                    keyword_hits.append(i)
                    break
        merged = sorted(recent + keyword_hits, key=lambda x: x.get("datetime", 0), reverse=True)
        return [{"headline": i.get("headline", ""), "source": i.get("source", ""),
                 "time": datetime.fromtimestamp(i.get("datetime", 0), tz=timezone.utc)
                                 .strftime("%m-%d %H:%M")} for i in merged]

    def get_recommendation_trend(self, symbol: str) -> list[dict]:
        """最近几个月的分析师评级趋势（strongBuy/buy/hold/sell/strongSell 人数）。"""
        data = self._get("/stock/recommendation", {"symbol": symbol})
        if not isinstance(data, list) or not data:
            return []
        # 返回最近 3 个月
        return data[:3]

    def get_upgrade_downgrade(self, symbol: str) -> list[dict]:
        """近期评级变动（升级/降级），返回最近 5 条。"""
        data = self._get("/stock/upgrade-downgrade", {"symbol": symbol})
        if not isinstance(data, list):
            return []
        items = sorted(data, key=lambda x: x.get("gradeDate", ""), reverse=True)[:5]
        result = []
        for item in items:
            action = item.get("action", "")
            result.append({
                "date":     item.get("gradeDate", ""),
                "company":  item.get("company", ""),
                "from":     item.get("fromGrade", ""),
                "to":       item.get("toGrade", ""),
                "action":   action,
            })
        return result

    def get_basic_financials(self, symbol: str) -> dict:
        """关键财务指标：PE、EPS增长、毛利率、收入增速等。"""
        data = self._get("/stock/metric", {"symbol": symbol, "metric": "all"})
        if not isinstance(data, dict):
            return {}
        m = data.get("metric", {})
        return {
            "pe_ttm":            m.get("peNormalizedAnnual"),
            "eps_growth_ttm":    m.get("epsGrowthTTMYoy"),
            "revenue_growth_ttm": m.get("revenueGrowthTTMYoy"),
            "gross_margin":      m.get("grossMarginTTM"),
            "roa":               m.get("roaTTM"),
            "roe":               m.get("roeTTM"),
            "debt_equity":       m.get("totalDebt/totalEquityAnnual"),
            "beta":              m.get("beta"),
            "52w_high":          m.get("52WeekHigh"),
            "52w_low":           m.get("52WeekLow"),
            "52w_return":        m.get("52WeekPriceReturnDaily"),
        }

    def get_earnings_calendar(self, from_date: str, to_date: str) -> set[str]:
        """
        返回 [from_date, to_date] 区间内即将公布财报的股票代码集合（全市场，一次调用）。
        用于初筛阶段批量排除临近财报的候选，不用逐个股票查询。
        """
        data = self._get("/calendar/earnings", {"from": from_date, "to": to_date})
        rows = data.get("earningsCalendar", []) if isinstance(data, dict) else []
        return {row["symbol"] for row in rows if row.get("symbol")}

    def get_earnings_surprise(self, symbol: str) -> list[dict]:
        """近4季度财报超预期情况（EPS实际 vs 预期）。"""
        data = self._get("/stock/earnings", {"symbol": symbol})
        if not isinstance(data, list):
            return []
        result = []
        for item in data[:4]:
            actual   = item.get("actual")
            estimate = item.get("estimate")
            surprise = item.get("surprisePercent")
            result.append({
                "period":   item.get("period", ""),
                "actual":   actual,
                "estimate": estimate,
                "surprise": surprise,    # 正数=超预期，负数=不及预期
            })
        return result
