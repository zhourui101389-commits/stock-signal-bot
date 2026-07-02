"""主动推送信号到所有订阅者。"""
import logging
from telegram import Bot
from telegram.constants import ParseMode
from src.analysis.signals import SignalResult
from src.notifications.formatter import format_signal_message
from src.storage import watchlist_repo, signal_repo

logger = logging.getLogger(__name__)


async def push_signal(bot: Bot, result: SignalResult, min_strength: int = 30,
                      pinned: bool = False) -> bool:
    """推送信号，返回 True 表示实际发送成功（供调度器追踪）。
    pinned=True 时即使 NEUTRAL 也强制推送（固定日报股票）。
    """
    if result.direction == "NEUTRAL" and not pinned:
        return False
    if result.strength < min_strength and not pinned:
        return False

    chat_ids = watchlist_repo.get_all_chat_ids()
    if not chat_ids:
        logger.warning("没有订阅者，跳过推送 %s", result.symbol)
        return

    signal_id = signal_repo.save_signal(
        symbol=result.symbol,
        direction=result.direction,
        strength=result.strength,
        close_price=result.close_price,
        rsi=result.rsi,
        reasons=result.reasons,
    )

    text = format_signal_message(result, pinned=pinned)
    sent = False
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            logger.info("已推送 %s 信号到 chat_id=%s", result.symbol, chat_id)
            sent = True
        except Exception as e:
            logger.error("推送到 %s 失败: %s", chat_id, e)

    signal_repo.mark_notified(signal_id)
    return sent
