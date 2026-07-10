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

    def _poll_fill(self, order_id, timeout_s: float = 8.0, interval_s: float = 0.5) -> dict:
        """
        轮询订单直到完全成交(filled)或超时，返回 {filled_qty, filled_price, status}。
        只有 status == "filled" 才算完成；partially_filled 视为未完成继续等，
        超时/终止时返回目前已知的部分成交信息（可能是0）供调用方自行判断。
        """
        import time
        deadline = time.monotonic() + timeout_s
        last_status = "unknown"
        last_filled_qty = 0.0
        last_filled_price = None
        while time.monotonic() < deadline:
            try:
                o = self._client.get_order_by_id(order_id)
            except Exception as e:
                logger.warning("查询订单状态失败 %s: %s", order_id, e)
                break
            last_status = o.status.value if hasattr(o.status, "value") else str(o.status)
            if o.filled_qty:
                last_filled_qty = float(o.filled_qty)
            if o.filled_avg_price:
                last_filled_price = float(o.filled_avg_price)
            if last_status == "filled":
                break
            if last_status in ("canceled", "expired", "rejected"):
                break
            time.sleep(interval_s)
        return {"filled_qty": last_filled_qty, "filled_price": last_filled_price, "status": last_status}

    def place_market_order(self, symbol: str, qty: int, side: str = "buy") -> Optional[dict]:
        """
        简单市价单，不带括号止盈止损，用于手动验证/测试单。
        出场时机由使用者自行决定（后续手动调用 close_position）。
        提交后轮询到成交，返回实际成交价/股数，供详细记账用。
        """
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if qty <= 0:
            logger.warning("下单数量为0，跳过 %s", symbol)
            return None
        if side not in ("buy", "sell"):
            logger.error("side参数非法(%r)，必须是'buy'或'sell'，跳过 %s，避免误判方向", side, symbol)
            return None

        try:
            order = self._client.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            fill = self._poll_fill(order.id)
            logger.info("市价单成功 %s %s ×%d 成交价%s", side, symbol, qty, fill["filled_price"])
            return {
                "order_id":     str(order.id),
                "symbol":       symbol,
                "qty":          qty,
                "side":         side,
                "filled_qty":   fill["filled_qty"],
                "filled_price": fill["filled_price"],
                "status":       fill["status"],
            }
        except Exception as e:
            logger.error("市价单失败 %s: %s", symbol, e)
            return None

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

    def place_extended_hours_order(
        self,
        symbol: str,
        qty: int,
        limit_price: float,
    ) -> Optional[dict]:
        """
        盘前盘后入场：纯限价单 + extended_hours=True。
        Alpaca 扩展时段只接受"纯限价单"，括号单(止损/止盈)在扩展时段一律拒收，
        所以这里下的是"裸单"——成交后没有服务端保护，止损止盈要等常规时段
        用 attach_protection() 补挂（见 _run_extended_watch 调用方）。
        """
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if qty <= 0:
            logger.warning("下单数量为0，跳过 %s", symbol)
            return None

        try:
            order = self._client.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(limit_price, 2),
                    extended_hours=True,
                )
            )
            logger.info("盘前盘后限价单成功 %s ×%d @%.2f（无保护，待补挂）",
                        symbol, qty, limit_price)
            return {
                "order_id": str(order.id),
                "symbol":   symbol,
                "qty":      qty,
                "entry":    limit_price,
                "status":   str(order.status),
            }
        except Exception as e:
            logger.error("盘前盘后下单失败 %s: %s", symbol, e)
            return None

    def attach_protection(
        self,
        symbol: str,
        qty: int,
        stop_loss: float,
        take_profit: float,
    ) -> Optional[dict]:
        """
        给已经持有、尚无保护的仓位补挂止损止盈（OCO 卖出对），只能在常规
        交易时段提交（OCO 同样不支持扩展时段）。用于盘前盘后裸单成交后、
        常规时段一开盘就补上保护。
        """
        from alpaca.trading.requests import (
            LimitOrderRequest, TakeProfitRequest, StopLossRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        if qty <= 0:
            return None
        if stop_loss >= take_profit:
            logger.warning("止损价(%.2f) >= 止盈价(%.2f)，跳过补挂 %s",
                           stop_loss, take_profit, symbol)
            return None

        try:
            order = self._client.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    limit_price=round(take_profit, 2),
                    order_class=OrderClass.OCO,
                    take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
                    stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
                )
            )
            logger.info("补挂保护成功 %s ×%d 止损%.2f 止盈%.2f",
                        symbol, qty, stop_loss, take_profit)
            return {
                "order_id": str(order.id),
                "symbol":   symbol,
                "qty":      qty,
                "stop":     stop_loss,
                "target":   take_profit,
                "status":   str(order.status),
            }
        except Exception as e:
            logger.error("补挂保护失败 %s: %s", symbol, e)
            return None

    def close_position(self, symbol: str) -> Optional[dict]:
        """全部平仓，轮询到成交，返回实际成交价/股数供详细记账用；失败返回None。"""
        try:
            order = self._client.close_position(symbol)
            fill = self._poll_fill(order.id)
            logger.info("已平仓 %s 成交价%s", symbol, fill["filled_price"])
            return {
                "order_id":     str(order.id),
                "symbol":       symbol,
                "filled_qty":   fill["filled_qty"],
                "filled_price": fill["filled_price"],
                "status":       fill["status"],
            }
        except Exception as e:
            logger.error("平仓失败 %s: %s", symbol, e)
            return None

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
