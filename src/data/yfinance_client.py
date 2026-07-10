"""
Yahoo Finance 数据客户端（替换 Moomoo OpenD）。
无需本地依赖，支持云端部署。

不支持的 Moomoo 专有功能（返回空值/NaN，评分层会自动跳过）：
  - 超大单/大单资金流向
  - Moomoo 期权/技术/财务异动信号
  - Morningstar 评级（改用分析师共识替代）
"""
import logging
import math
import time
import functools
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_NAN = float("nan")


def _retry(func):
    """简单重试装饰器：yfinance 偶发网络抖动时自动重试一次。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.warning("%s 第一次失败（%s），1秒后重试", func.__name__, e)
            time.sleep(1)
            try:
                return func(*args, **kwargs)
            except Exception as e2:
                logger.error("%s 最终失败: %s", func.__name__, e2)
                return None
    return wrapper


def _to_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    col_map = {"Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"}
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    df.index = pd.to_datetime(df.index).normalize().tz_localize(None)
    df.index.name = "date"
    needed = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[needed].sort_index()


class YFinanceDataClient:
    """Yahoo Finance 数据客户端，接口与 MoomooDataClient 保持一致。"""

    # ──────────────────────────────────────────────
    # K 线数据
    # ──────────────────────────────────────────────

    def get_daily_bars(self, symbol: str, limit: int = 250) -> pd.DataFrame:
        days = int(limit * 1.6)
        period = f"{max(days, 30)}d"
        try:
            df = yf.Ticker(symbol).history(period=period, auto_adjust=True)
            df = _to_ohlcv(df)
            return df.tail(limit) if not df.empty else df
        except Exception as e:
            logger.error("get_daily_bars %s 失败: %s", symbol, e)
            return pd.DataFrame()

    def get_weekly_bars(self, symbol: str, limit: int = 104) -> pd.DataFrame:
        try:
            df = yf.Ticker(symbol).history(period="3y", interval="1wk", auto_adjust=True)
            df = _to_ohlcv(df)
            return df.tail(limit) if not df.empty else df
        except Exception as e:
            logger.error("get_weekly_bars %s 失败: %s", symbol, e)
            return pd.DataFrame()

    # ──────────────────────────────────────────────
    # 自选股列表（返回空，由 SQLite watchlist_repo 接管）
    # ──────────────────────────────────────────────

    def get_watchlist(self, group: str = "US") -> list[str]:
        return []

    # ──────────────────────────────────────────────
    # 实时行情快照
    # ──────────────────────────────────────────────

    def get_quote(self, symbol: str) -> dict:
        try:
            t = yf.Ticker(symbol)
            info = t.info or {}

            prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose") or _NAN
            current    = (info.get("currentPrice")
                          or info.get("regularMarketPrice")
                          or info.get("ask")
                          or _NAN)

            today_chg = _NAN
            if not (math.isnan(float(current)) or math.isnan(float(prev_close))) and float(prev_close) > 0:
                today_chg = (float(current) - float(prev_close)) / float(prev_close) * 100

            vol     = info.get("volume") or info.get("regularMarketVolume") or _NAN
            avg_vol = info.get("averageVolume10days") or info.get("averageVolume") or 0
            vol_ratio = (float(vol) / float(avg_vol)) if avg_vol and not math.isnan(float(vol)) else _NAN

            return {
                "symbol":           symbol,
                "latest_price":     current,
                "prev_close":       prev_close,
                "open":             info.get("open") or info.get("regularMarketOpen"),
                "high":             info.get("dayHigh") or info.get("regularMarketDayHigh"),
                "low":              info.get("dayLow") or info.get("regularMarketDayLow"),
                "today_change_pct": today_chg,
                "volume":           vol,
                "volume_ratio":     vol_ratio,
                "turnover_rate":    _NAN,
                "pre_change_pct":   _NAN,   # yfinance 盘前数据不稳定，不用
                "pre_price":        _NAN,
                "after_change_pct": _NAN,
                "bid_ask_ratio":    _NAN,
                "pe_ttm":           info.get("trailingPE", _NAN),
                "pb":               info.get("priceToBook", _NAN),
                "week52_high":      info.get("fiftyTwoWeekHigh", _NAN),
                "week52_low":       info.get("fiftyTwoWeekLow", _NAN),
                "amplitude":        _NAN,
            }
        except Exception as e:
            logger.error("get_quote %s 失败: %s", symbol, e)
            return {"symbol": symbol}

    # ──────────────────────────────────────────────
    # 盘前/盘后最新成交（用于扩展时段哨兵检测）
    # ──────────────────────────────────────────────

    def get_extended_hours_quote(self, symbol: str) -> dict:
        """
        盘前/盘后最新成交价：复用 1 分钟线 + prepost=True 的方式获取，而不是
        .info 里经常返回 None 的 preMarketPrice/postMarketPrice 字段（后者在
        get_quote() 里已经因为不稳定被禁用）。这个 1 分钟线机制已经在
        _is_us_trading_day() 里跑了很久证明可用，这里复用同一套思路。
        """
        try:
            hist = yf.Ticker(symbol).history(period="1d", interval="1m", prepost=True)
            if hist.empty:
                return {}
            latest_price = float(hist["Close"].iloc[-1])
            latest_ts    = hist.index[-1]

            info = yf.Ticker(symbol).info or {}
            prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
            if not prev_close:
                return {}
            prev_close = float(prev_close)
            change_pct = (latest_price - prev_close) / prev_close * 100

            return {
                "symbol":     symbol,
                "price":      latest_price,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "timestamp":  latest_ts,
            }
        except Exception as e:
            logger.warning("get_extended_hours_quote %s 失败: %s", symbol, e)
            return {}

    # ──────────────────────────────────────────────
    # 资金流向（不支持，返回空）
    # ──────────────────────────────────────────────

    def get_capital_flow_summary(self, symbol: str) -> dict:
        return {}

    # ──────────────────────────────────────────────
    # 估值水位
    # ──────────────────────────────────────────────

    def get_valuation_summary(self, symbol: str) -> dict:
        try:
            info = yf.Ticker(symbol).info or {}
            return {
                "pe_current":    info.get("trailingPE", _NAN),
                "pe_5yr_avg":    _NAN,
                "pe_percentile": _NAN,
                "pe_forward":    info.get("forwardPE", _NAN),
            }
        except Exception as e:
            logger.error("get_valuation_summary %s 失败: %s", symbol, e)
            return {}

    # ──────────────────────────────────────────────
    # Morningstar（不支持，返回空）
    # ──────────────────────────────────────────────

    def get_morningstar_summary(self, symbol: str) -> dict:
        return {}

    # ──────────────────────────────────────────────
    # 做空数据
    # ──────────────────────────────────────────────

    def get_short_summary(self, symbol: str) -> dict:
        try:
            info = yf.Ticker(symbol).info or {}
            short_pct = info.get("shortPercentOfFloat")
            short_ratio = info.get("shortRatio")
            return {
                "short_pct":     float(short_pct) * 100 if short_pct else _NAN,
                "days_to_cover": float(short_ratio) if short_ratio else _NAN,
                "date":          "",
            }
        except Exception as e:
            logger.error("get_short_summary %s 失败: %s", symbol, e)
            return {}

    # ──────────────────────────────────────────────
    # 期权/技术/财务异动（不支持，返回空）
    # ──────────────────────────────────────────────

    def get_unusual_signals(self, symbol: str) -> dict:
        return {}

    # ──────────────────────────────────────────────
    # 财报日期 + 历史
    # ──────────────────────────────────────────────

    def get_earnings_summary(self, symbol: str) -> dict:
        try:
            t = yf.Ticker(symbol)

            # 下次财报日
            next_date = ""
            try:
                cal = t.calendar
                if cal is not None and not cal.empty and "Earnings Date" in cal.index:
                    val = cal.loc["Earnings Date"]
                    dates = val if hasattr(val, "__iter__") and not isinstance(val, str) else [val]
                    future = [d for d in dates if pd.Timestamp(d) >= pd.Timestamp.now()]
                    if future:
                        next_date = str(future[0])[:10]
            except Exception:
                pass

            # 历史财报（实际 vs 预估 EPS）
            history = []
            try:
                hist = t.earnings_history
                if hist is not None and not hist.empty:
                    for idx, row in hist.tail(6).iterrows():
                        history.append({
                            "period":       str(idx)[:7],
                            "date":         str(idx)[:10],
                            "move":         _NAN,
                            "actual_eps":   row.get("epsActual"),
                            "estimate":     row.get("epsEstimate"),
                        })
            except Exception:
                pass

            return {"history": history, "next_date": next_date}
        except Exception as e:
            logger.error("get_earnings_summary %s 失败: %s", symbol, e)
            return {}

    # ──────────────────────────────────────────────
    # 深度报告（云端版不支持，返回空）
    # ──────────────────────────────────────────────

    def get_deep_report(self, symbol: str) -> dict:
        return {}

    # ──────────────────────────────────────────────
    # 分析师共识
    # ──────────────────────────────────────────────

    def get_analyst_consensus(self, symbol: str) -> dict:
        try:
            info = yf.Ticker(symbol).info or {}
            rec_mean    = info.get("recommendationMean")   # 1=强买 … 5=强卖
            num_analysts = info.get("numberOfAnalystOpinions") or 0
            target_mean  = info.get("targetMeanPrice")
            target_high  = info.get("targetHighPrice")
            target_low   = info.get("targetLowPrice")

            # 把推荐均值近似映射成买入%
            buy_pct = _NAN
            if rec_mean is not None:
                if   rec_mean <= 1.5: buy_pct = 90.0
                elif rec_mean <= 2.0: buy_pct = 78.0
                elif rec_mean <= 2.5: buy_pct = 62.0
                elif rec_mean <= 3.0: buy_pct = 45.0
                elif rec_mean <= 3.5: buy_pct = 28.0
                else:                 buy_pct = 12.0

            return {
                "analyst_buy_pct":     buy_pct,
                "analyst_hold_pct":    _NAN,
                "analyst_sell_pct":    _NAN,
                "analyst_total":       int(num_analysts),
                "analyst_target_avg":  float(target_mean) if target_mean else _NAN,
                "analyst_target_high": float(target_high) if target_high else _NAN,
                "analyst_target_low":  float(target_low)  if target_low  else _NAN,
                "analyst_rating":      float(rec_mean) if rec_mean else 0,
            }
        except Exception as e:
            logger.error("get_analyst_consensus %s 失败: %s", symbol, e)
            return {}

    # ──────────────────────────────────────────────
    # 内部人士交易
    # ──────────────────────────────────────────────

    def get_insider_trades(self, symbol: str, limit: int = 5) -> list[dict]:
        try:
            df = yf.Ticker(symbol).insider_transactions
            if df is None or df.empty:
                return []
            result = []
            for _, row in df.head(limit).iterrows():
                shares = row.get("Shares") or row.get("shares") or 0
                if not shares or math.isnan(float(shares)):
                    continue
                shares = int(float(shares))
                txn = str(row.get("Transaction") or row.get("transaction") or "")
                is_buy = any(k in txn for k in ("Purchase", "Buy", "Acquisition", "Grant", "Award"))
                is_sell = any(k in txn for k in ("Sale", "Sell", "Disposition"))
                if is_buy:
                    action = "买入"
                elif is_sell:
                    action = "卖出"
                else:
                    # 兜底：用 shares 符号判断（正=买入）
                    action = "买入" if shares > 0 else "卖出"
                result.append({
                    "name":   str(row.get("Insider") or row.get("insider") or ""),
                    "title":  str(row.get("Position") or row.get("position") or ""),
                    "shares": abs(shares),
                    "date":   str(row.get("Start Date") or row.get("date") or "")[:10],
                    "action": action,
                    "txn":    txn,
                })
            return result
        except Exception as e:
            logger.error("get_insider_trades %s 失败: %s", symbol, e)
            return []
