"""
云端扫描脚本（GitHub Actions 调用）。
每天 21:00 CST 自动运行，完成后退出。
依赖环境变量：TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS（逗号分隔的 chat_id 列表）
"""
import asyncio
import logging
import os
import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from telegram import Bot
from telegram.constants import ParseMode

from src.config import Config
from src.storage.database import init_db
from src.storage import watchlist_repo
from src.data.yfinance_client import YFinanceDataClient
from src.analysis.multi_timeframe import analyze_symbol
from src.notifications.formatter import format_signal_message, format_economic_calendar, format_serenity_section
from src.data.serenity_tracker import get_serenity_picks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _get_chat_ids(config: Config) -> list[int]:
    """从环境变量或 SQLite 获取推送目标 chat_id 列表。"""
    env_ids = os.environ.get("TELEGRAM_CHAT_IDS", "")
    if env_ids:
        return [int(x.strip()) for x in env_ids.split(",") if x.strip()]
    return watchlist_repo.get_all_chat_ids()


async def main():
    config = Config()
    init_db(config.DB_PATH)

    # 保证自选股列表完整
    all_symbols = list(dict.fromkeys(
        config.tier_core + config.tier_swing
        + config.tier_speculative + config.pinned_symbols
    ))
    watchlist_repo.seed_defaults(all_symbols or config.watchlist_defaults)
    # 补充缺失的股票
    existing = set(watchlist_repo.get_all_symbols())
    for sym in all_symbols:
        if sym not in existing:
            watchlist_repo.add_symbol(sym)

    symbols   = watchlist_repo.get_all_symbols()
    chat_ids  = _get_chat_ids(config)
    pinned    = set(config.pinned_symbols)
    min_str   = config.signals.get("min_strength", 30)

    if not symbols:
        logger.error("自选股列表为空，退出")
        return
    if not chat_ids:
        logger.error("无推送目标 chat_id，退出")
        return

    logger.info("开始扫描 %d 只股票 → 推送到 %d 个 chat", len(symbols), len(chat_ids))

    client = YFinanceDataClient()
    bot    = Bot(token=config.TELEGRAM_BOT_TOKEN)

    # ── Serenity 板块观点 ─────────────────────────
    try:
        picks = get_serenity_picks()
        serenity_text = format_serenity_section(picks)
        if serenity_text:
            for chat_id in chat_ids:
                await bot.send_message(chat_id=chat_id, text=serenity_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning("Serenity 获取失败: %s", e)

    # ── 逐只扫描并推送 ────────────────────────────
    pushed = 0
    for sym in symbols:
        try:
            result = analyze_symbol(
                client, sym,
                config.signals.get("lookback_days", 250),
                config.signals.get("lookback_weeks", 104),
                config.total_capital, config.max_position_pct,
            )
            is_pinned = sym in pinned
            result.pinned = is_pinned
            effective_min = 0 if is_pinned else min_str
            if result.strength < effective_min:
                logger.info("跳过 %s: %s 强度%d", sym, result.direction, result.strength)
                continue
            text = format_signal_message(result, pinned=is_pinned)
            for chat_id in chat_ids:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            logger.info("✅ 推送 %s: %s 强度%d", sym, result.direction, result.strength)
            pushed += 1
            await asyncio.sleep(0.5)   # 避免 Telegram 限速
        except Exception as e:
            logger.error("处理 %s 失败: %s", sym, e)

    logger.info("扫描完成，共推送 %d 条信号", pushed)


if __name__ == "__main__":
    asyncio.run(main())
