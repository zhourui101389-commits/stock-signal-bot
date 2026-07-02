"""
Moomoo (富途) OpenAPI 数据客户端。

前提：本地需要运行 OpenD 守护进程（默认 127.0.0.1:11111）。
无需任何 API Key，OpenD 登录 Moomoo 账号后即可使用。
"""
import logging
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)


def _kline_to_df(data: pd.DataFrame) -> pd.DataFrame:
    """将 Moomoo request_history_kline 返回的 DataFrame 转换为标准 OHLCV 格式。"""
    df = data[["time_key", "open", "high", "low", "close", "volume"]].copy()
    df.loc[:, "date"] = pd.to_datetime(df["time_key"]).dt.normalize()
    return df.drop(columns=["time_key"]).set_index("date").sort_index()


class MoomooDataClient:
    """Moomoo OpenQuoteContext 封装，对外只暴露标准 DataFrame 接口。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111) -> None:
        self.host = host
        self.port = port
        logger.info("MoomooDataClient 初始化，OpenD 地址: %s:%d", host, port)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get_daily_bars(self, symbol: str, limit: int = 250) -> pd.DataFrame:
        """获取日线 K 线，返回最近 limit 根 K 线的 OHLCV DataFrame。"""
        end = datetime.now().strftime("%Y-%m-%d")
        # 用 1.6 倍日历天数保证覆盖足够的交易日（含节假日）
        start = (datetime.now() - timedelta(days=int(limit * 1.6))).strftime("%Y-%m-%d")
        return self._fetch_bars(symbol, ktype="K_DAY", start=start, end=end, limit=limit)

    def get_weekly_bars(self, symbol: str, limit: int = 104) -> pd.DataFrame:
        """获取周线 K 线，返回最近 limit 根周线的 OHLCV DataFrame。"""
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=int(limit * 7 * 1.2))).strftime("%Y-%m-%d")
        return self._fetch_bars(symbol, ktype="K_WEEK", start=start, end=end, limit=limit)

    def get_watchlist(self, group: str = "US") -> list[str]:
        """从 Moomoo 账户自选股分组拉取美股代码列表。"""
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, data = ctx.get_user_security(group)
            if ret != 0 or data.empty:
                logger.warning("获取自选股分组 [%s] 失败，ret=%d", group, ret)
                return []
            symbols = (
                data[data["code"].str.startswith("US.")]
                ["code"]
                .str.replace("US.", "", regex=False)
                .tolist()
            )
            logger.info("从 Moomoo [%s] 分组读取到 %d 只美股: %s", group, len(symbols), symbols)
            return symbols
        except Exception as e:
            logger.error("获取自选股分组异常: %s", e)
            return []
        finally:
            ctx.close()

    def get_quote(self, symbol: str) -> dict:
        """返回单只股票完整行情快照（实时价、盘前盘后、量比、估值等）。"""
        from moomoo import OpenQuoteContext

        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, data = ctx.get_market_snapshot([f"US.{symbol}"])
            if ret != 0 or data.empty:
                logger.warning("获取 %s 行情失败，ret=%d", symbol, ret)
                return {"symbol": symbol}
            r = data.iloc[0]
            return {
                "symbol": symbol,
                # 实时价格
                "latest_price":     r.get("last_price"),
                "prev_close":       r.get("prev_close_price"),
                "open":             r.get("open_price"),
                "high":             r.get("high_price"),
                "low":              r.get("low_price"),
                "today_change_pct": (r.get("last_price", 0) - r.get("prev_close_price", 0))
                                    / r.get("prev_close_price", 1) * 100,
                # 量能
                "volume":           r.get("volume"),
                "volume_ratio":     r.get("volume_ratio"),      # 量比（vs 10日均量）
                "turnover_rate":    r.get("turnover_rate"),     # 换手率
                # 盘前数据（美股盘前 4:00-9:30 ET）
                "pre_change_pct":   r.get("pre_change_rate"),   # 盘前涨跌幅 %
                "pre_price":        r.get("pre_price"),
                # 盘后数据
                "after_change_pct": r.get("after_change_rate"),
                # 买卖盘情绪（正=买盘强，负=卖盘强）
                "bid_ask_ratio":    r.get("bid_ask_ratio"),
                # 估值
                "pe_ttm":           r.get("pe_ttm_ratio"),
                "pb":               r.get("pb_ratio"),
                # 52周高低
                "week52_high":      r.get("highest52weeks_price"),
                "week52_low":       r.get("lowest52weeks_price"),
                # 振幅（今日 high-low / prev_close）
                "amplitude":        r.get("amplitude"),
            }
        except Exception as e:
            logger.error("获取 %s 实时行情异常: %s", symbol, e)
            return {"symbol": symbol}
        finally:
            ctx.close()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def get_capital_flow_summary(self, symbol: str) -> dict:
        """返回今日大资金净流入（超大单/大单/小单），正=净流入，负=净流出。"""
        from moomoo import OpenQuoteContext
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, df = ctx.get_capital_distribution(f"US.{symbol}")
            if ret != 0 or df.empty:
                return {}
            r = df.iloc[0]
            return {
                "net_super": float(r["capital_in_super"] - r["capital_out_super"]),
                "net_big":   float(r["capital_in_big"]   - r["capital_out_big"]),
                "net_mid":   float(r["capital_in_mid"]   - r["capital_out_mid"]),
                "net_small": float(r["capital_in_small"] - r["capital_out_small"]),
            }
        except Exception as e:
            logger.error("获取 %s 资金分布异常: %s", symbol, e)
            return {}
        finally:
            ctx.close()

    def get_valuation_summary(self, symbol: str) -> dict:
        """返回估值趋势：当前PE、5年均值、历史百分位、远期PE。"""
        from moomoo import OpenQuoteContext
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, data = ctx.get_valuation_detail(f"US.{symbol}")
            if ret != 0 or not data:
                return {}
            t = data.get("trend", {})
            return {
                "pe_current":    t.get("current_value", float("nan")),
                "pe_5yr_avg":    t.get("average_value", float("nan")),
                "pe_percentile": t.get("valuation_percentile", float("nan")),
                "pe_forward":    t.get("forward_value", float("nan")),
            }
        except Exception as e:
            logger.error("获取 %s 估值详情异常: %s", symbol, e)
            return {}
        finally:
            ctx.close()

    def get_morningstar_summary(self, symbol: str) -> dict:
        """返回 Morningstar 评级、公允价值、护城河、最新分析师观点标题。"""
        from moomoo import OpenQuoteContext
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, data = ctx.get_research_morningstar_report(f"US.{symbol}")
            if ret != 0 or not data:
                return {}
            bull_says = [b["context"] for b in data.get("bull_say", [])[:2]]
            bear_says = [b["context"] for b in data.get("bear_say", [])[:2]]
            note = data.get("analyst_note_title", {})
            return {
                "stars":       data.get("star_rating", 0),
                "fair_value":  data.get("fair_value", float("nan")),
                "moat":        data.get("economic_moat_label", ""),
                "note_title":  note.get("context", "") if isinstance(note, dict) else "",
                "note_date":   note.get("update_time_str", "") if isinstance(note, dict) else "",
                "bull_says":   bull_says,
                "bear_says":   bear_says,
            }
        except Exception as e:
            logger.error("获取 %s Morningstar异常: %s", symbol, e)
            return {}
        finally:
            ctx.close()

    def get_short_summary(self, symbol: str) -> dict:
        """返回最新做空比例和回补天数。"""
        from moomoo import OpenQuoteContext
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            result = ctx.get_short_interest(f"US.{symbol}", num=1)
            ret, df = result[0], result[1]
            if ret != 0 or df.empty:
                return {}
            r = df.iloc[0]
            return {
                "short_pct":      float(r["short_percent"]),
                "days_to_cover":  float(r["days_to_cover"]),
                "date":           str(r.get("timestamp_str", "")),
            }
        except Exception as e:
            logger.error("获取 %s 做空数据异常: %s", symbol, e)
            return {}
        finally:
            ctx.close()

    def get_unusual_signals(self, symbol: str) -> dict:
        """返回近7日期权大单、财务、技术异动文字（无异动时返回空字符串）。"""
        from moomoo import OpenQuoteContext
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            r1 = ctx.get_derivative_unusual(f"US.{symbol}")
            r2 = ctx.get_financial_unusual(f"US.{symbol}")
            r3 = ctx.get_technical_unusual(f"US.{symbol}")

            def _extract(ret, data):
                if ret != 0 or not isinstance(data, dict):
                    return ""
                if data.get("err_code", 1) != 0:
                    return ""
                return data.get("content", "").strip()

            options_text  = _extract(r1[0], r1[1]) if r1[0] == 0 else ""
            financial_text = _extract(r2[0], r2[1]) if r2[0] == 0 else ""
            technical_text = _extract(r3[0], r3[1]) if r3[0] == 0 else ""
            return {
                "options":   options_text,
                "financial": financial_text,
                "technical": technical_text,
            }
        except Exception as e:
            logger.error("获取 %s 异动信号异常: %s", symbol, e)
            return {}
        finally:
            ctx.close()

    def get_earnings_summary(self, symbol: str) -> dict:
        """返回下次财报日 + 最近6季财报当天涨跌幅。"""
        from moomoo import OpenQuoteContext
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, df = ctx.get_financials_earnings_price_history(f"US.{symbol}")
            if ret != 0 or df.empty:
                return {}

            # 每季取首条记录（发布当天），按 pub_trading_day 升序后取最近6季
            by_period = (
                df.sort_values("pub_trading_day")
                  .groupby("period_text", sort=False)
                  .first()
                  .reset_index()
            )
            move = (by_period["close_price"] - by_period["last_close_price"]) / by_period["last_close_price"] * 100
            by_period = by_period.copy()
            by_period.loc[:, "move_pct"] = move
            recent = by_period.tail(6)[["period_text", "pub_trading_day_str", "move_pct"]].copy()
            history = [
                {
                    "period": row["period_text"],
                    "date":   row["pub_trading_day_str"],
                    "move":   round(float(row["move_pct"]), 2),
                }
                for _, row in recent.iterrows()
            ]

            # 下次财报：is_current=True 的最新一条
            next_date = ""
            if "is_current" in df.columns:
                current = df[df["is_current"] == True]
                if not current.empty:
                    next_date = current.iloc[0].get("pub_trading_day_str", "")

            return {"history": history, "next_date": next_date}
        except Exception as e:
            logger.error("获取 %s 财报历史异常: %s", symbol, e)
            return {}
        finally:
            ctx.close()

    def get_deep_report(self, symbol: str) -> dict:
        """深度报告：收入拆分 + 分红 + 股东结构 + 运营效率。"""
        from moomoo import OpenQuoteContext
        import json as _json
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        result = {}
        try:
            # 收入拆分（最新季度）
            ret, data = ctx.get_financials_revenue_breakdown(f"US.{symbol}")
            if ret == 0 and data:
                period = data.get("period", "")
                segs = data.get("breakdown_list", [{}])[0].get("item_list", [])
                result["revenue_breakdown"] = {
                    "period": period,
                    "segments": [{"name": s["name"], "pct": round(s["ratio"], 1)} for s in segs],
                }

            # 分红（最近5次）
            ret2, data2 = ctx.get_corporate_actions_dividends(f"US.{symbol}")
            if ret2 == 0 and data2:
                divs = data2.get("dividend_list", [])[:5]
                result["dividends"] = divs

            # 股东结构
            ret3, data3 = ctx.get_shareholders_overview(f"US.{symbol}")
            if ret3 == 0 and data3:
                import pandas as pd, io
                holder_type_raw = data3.get("holder_type", "")
                if isinstance(holder_type_raw, str):
                    try:
                        ht_df = pd.read_csv(io.StringIO(holder_type_raw), sep=r'\s{2,}', engine='python', skipinitialspace=True)
                    except Exception:
                        ht_df = pd.DataFrame()
                elif isinstance(holder_type_raw, pd.DataFrame):
                    ht_df = holder_type_raw
                else:
                    ht_df = pd.DataFrame()
                if not ht_df.empty and "name" in ht_df.columns and "holder_pct" in ht_df.columns:
                    result["holder_types"] = [
                        {"type": row["name"], "pct": round(float(row["holder_pct"]), 2)}
                        for _, row in ht_df.iterrows()
                    ]

            # 机构持仓趋势（最近4季）
            ret4, df4 = ctx.get_shareholders_institutional(f"US.{symbol}")
            if ret4 == 0 and isinstance(df4, pd.DataFrame if 'pd' in dir() else type(None), ) and not df4.empty:
                pass  # handled below
            if ret4 == 0:
                import pandas as pd
                if isinstance(df4, pd.DataFrame) and not df4.empty:
                    result["inst_trend"] = [
                        {
                            "period": row["period_text"],
                            "count":  int(row["institution_quantity"]),
                            "count_chg": int(row["institution_quantity_change"]),
                            "pct":    round(float(row["holder_pct"]), 2),
                            "pct_chg": round(float(row["holder_pct_change"]), 3),
                        }
                        for _, row in df4.head(4).iterrows()
                    ]

            # 运营效率（最新年度）
            ret5, data5 = ctx.get_company_operational_efficiency(f"US.{symbol}")
            if ret5 == 0 and data5:
                items = data5.get("item_list", [])
                if items:
                    latest = items[0]
                    result["op_efficiency"] = {
                        "period":    latest.get("period_text", ""),
                        "employees": int(latest.get("employee_num", 0)),
                        "emp_yoy":   round(float(latest.get("employee_num_yoy", 0)), 1),
                        "revenue_per_emp": int(latest.get("income_per_capita", 0)),
                        "profit_per_emp":  int(latest.get("net_profit_per_capita", 0)),
                    }

        except Exception as e:
            logger.error("获取 %s 深度报告异常: %s", symbol, e)
        finally:
            ctx.close()
        return result

    def get_analyst_consensus(self, symbol: str) -> dict:
        """返回分析师共识：买/持/卖比例、平均目标价、最高最低目标价。"""
        from moomoo import OpenQuoteContext
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, data = ctx.get_research_analyst_consensus(f"US.{symbol}")
            if ret != 0 or not data:
                return {}
            return {
                "analyst_buy_pct":    data.get("buy", float("nan")),
                "analyst_hold_pct":   data.get("hold", float("nan")),
                "analyst_sell_pct":   data.get("sell", float("nan")),
                "analyst_total":      data.get("total", 0),
                "analyst_target_avg": data.get("average", float("nan")),
                "analyst_target_high": data.get("highest", float("nan")),
                "analyst_target_low": data.get("lowest", float("nan")),
                "analyst_rating":     data.get("rating", 0),
            }
        except Exception as e:
            logger.error("获取 %s 分析师共识异常: %s", symbol, e)
            return {}
        finally:
            ctx.close()

    def get_insider_trades(self, symbol: str, limit: int = 5) -> list[dict]:
        """返回最近内部人士交易（高管用自己的钱买卖股票）。"""
        from moomoo import OpenQuoteContext
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, df = ctx.get_insider_trade_list(f"US.{symbol}")
            if ret != 0 or df.empty:
                return []
            trades = []
            for _, row in df.head(limit).iterrows():
                shares = int(row.get("trade_shares", 0))
                if shares == 0:
                    continue
                trades.append({
                    "name":      row.get("name", ""),
                    "title":     row.get("title", ""),
                    "shares":    shares,
                    "date":      row.get("min_trade_date_str", ""),
                    "action":    "买入" if shares > 0 else "卖出",
                })
            return trades
        except Exception as e:
            logger.error("获取 %s 内部人士交易异常: %s", symbol, e)
            return []
        finally:
            ctx.close()

    def _fetch_bars(self, symbol: str, ktype: str, start: str, end: str, limit: int) -> pd.DataFrame:
        from moomoo import OpenQuoteContext, KLType, AuType

        ktype_map = {
            "K_DAY": KLType.K_DAY,
            "K_WEEK": KLType.K_WEEK,
        }
        moomoo_ktype = ktype_map.get(ktype, KLType.K_DAY)

        ctx = OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, data, _ = ctx.request_history_kline(
                f"US.{symbol}",
                start=start,
                end=end,
                ktype=moomoo_ktype,
                autype=AuType.QFQ,   # 前复权
                max_count=limit,
            )
            if ret != 0:
                logger.warning("%s %s K线获取失败，ret=%d", symbol, ktype, ret)
                return pd.DataFrame()
            if data.empty:
                logger.warning("%s %s 返回空数据", symbol, ktype)
                return pd.DataFrame()
            return _kline_to_df(data)
        except Exception as e:
            logger.error("获取 %s %s K线异常: %s", symbol, ktype, e)
            return pd.DataFrame()
        finally:
            ctx.close()
