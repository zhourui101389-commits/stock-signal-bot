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

    def get_market_news(self, category: str = "general") -> list[dict]:
        """大盘宏观新闻（category: general / forex / crypto / merger）。"""
        data = self._get("/news", {"category": category})
        if not isinstance(data, list):
            return []
        items = sorted(data, key=lambda x: x.get("datetime", 0), reverse=True)[:8]
        return [{"headline": i.get("headline", ""), "source": i.get("source", ""),
                 "time": datetime.fromtimestamp(i.get("datetime", 0), tz=timezone.utc)
                                 .strftime("%m-%d %H:%M")} for i in items]

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
