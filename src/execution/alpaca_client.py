"""
Alpaca 模拟交易执行层。
使用括号单（bracket order）一次性设好入场 + 止损 + 止盈，
Alpaca 服务端自动监控并执行，无需本地进程常驻。
"""
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


class AlpacaClient:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        from alpaca.trading.client import TradingClient
        self._client = TradingClient(api_key, api_secret, paper=paper)

    def get_account(self) -> dict:
        acc = self._client.get_account()
        equity      = float(acc.equity)
        last_equity = float(acc.last_equity)
        today_pl    = equity - last_equity
        return {
            "equity":        equity,
            "cash":          float(acc.cash),
            "buying_power":  float(acc.buying_power),
            "today_pl":      today_pl,
            "today_pl_pct":  today_pl / last_equity * 100 if last_equity > 0 else 0,
        }

    def get_positions(self) -> list[dict]:
        positions = self._client.get_all_positions()
        result = []
        for p in positions:
            cur = float(p.current_price) if p.current_price else None
            result.append({
                "symbol":          p.symbol,
                "qty":             float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price":   cur,
                "market_value":    float(p.market_value),
                "unrealized_pl":   float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc) * 100,
            })
        return result

    def place_bracket_order(
        self,
        symbol: str,
        qty: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> Optional[dict]:
        """
        括号单：入场限价单 + 服务端止损 + 服务端止盈。
        Alpaca 不需要本地进程常驻，条件触发由其服务器处理。
        """
        from alpaca.trading.requests import (
            LimitOrderRequest, TakeProfitRequest, StopLossRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        if qty <= 0:
            logger.warning("下单数量为0，跳过 %s", symbol)
            return None
        if stop_loss >= entry_price:
            logger.warning("止损价(%.2f) >= 入场价(%.2f)，跳过 %s",
                           stop_loss, entry_price, symbol)
            return None
        if take_profit <= entry_price:
            logger.warning("目标价(%.2f) <= 入场价(%.2f)，跳过 %s",
                           take_profit, entry_price, symbol)
            return None

        try:
            order = self._client.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(entry_price, 2),
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
                    stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
                )
            )
            logger.info("括号单成功 %s ×%d @%.2f 止损%.2f 止盈%.2f",
                        symbol, qty, entry_price, stop_loss, take_profit)
            return {
                "order_id": str(order.id),
                "symbol":   symbol,
                "qty":      qty,
                "entry":    entry_price,
                "stop":     stop_loss,
                "target":   take_profit,
                "status":   str(order.status),
            }
        except Exception as e:
            logger.error("下单失败 %s: %s", symbol, e)
            return None

    def close_position(self, symbol: str) -> bool:
        try:
            self._client.close_position(symbol)
            logger.info("已平仓 %s", symbol)
            return True
        except Exception as e:
            logger.error("平仓失败 %s: %s", symbol, e)
            return False

    def get_closed_orders(self, limit: int = 50) -> list[dict]:
        """获取近期已成交或已关闭的订单，用于复盘回填"""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        try:
            orders = self._client.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit)
            )
            result = []
            for o in orders:
                result.append({
                    "order_id":    str(o.id),
                    "symbol":      o.symbol,
                    "side":        str(o.side),
                    "qty":         float(o.qty or 0),
                    "filled_qty":  float(o.filled_qty or 0),
                    "filled_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                    "status":      str(o.status),
                    "submitted_at": str(o.submitted_at),
                    "filled_at":   str(o.filled_at) if o.filled_at else None,
                })
            return result
        except Exception as e:
            logger.error("获取历史订单失败: %s", e)
            return []
