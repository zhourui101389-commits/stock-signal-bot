"""
云端扫描脚本（GitHub Actions 调用）。
每天 21:00 CST 自动运行，完成后退出。
依赖环境变量：TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, FINNHUB_API_KEY, ANTHROPIC_API_KEY
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
from src.data.finnhub_client import FinnhubClient
from src.analysis.multi_timeframe import analyze_symbol
from src.analysis.ai_analyst import run_ai_analysis
from src.notifications.formatter import (
    format_signal_message, format_ai_analysis,
    format_serenity_section,
)
from src.data.serenity_tracker import get_serenity_picks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _get_chat_ids(config: Config) -> list[int]:
    env_ids = os.environ.get("TELEGRAM_CHAT_IDS", "")
    if env_ids:
        return [int(x.strip()) for x in env_ids.split(",") if x.strip()]
    return watchlist_repo.get_all_chat_ids()


def _get_macro_context(finnhub: FinnhubClient | None) -> str:
    """拉取宏观市场新闻，组成一段背景描述给 AI。"""
    if not finnhub:
        return ""
    try:
        news = finnhub.get_market_news("general")
        if not news:
            return ""
        headlines = [f"- {n['headline']}" for n in news[:5]]
        return "当前宏观市场新闻：\n" + "\n".join(headlines)
    except Exception:
        return ""


async def main():
    config = Config()
    init_db(config.DB_PATH)

    all_symbols = list(dict.fromkeys(
        config.tier_core + config.tier_swing
        + config.tier_speculative + config.pinned_symbols
    ))
    watchlist_repo.seed_defaults(all_symbols or config.watchlist_defaults)
    existing = set(watchlist_repo.get_all_symbols())
    for sym in all_symbols:
        if sym not in existing:
            watchlist_repo.add_symbol(sym)

    symbols  = watchlist_repo.get_all_symbols()
    chat_ids = _get_chat_ids(config)
    pinned   = set(config.pinned_symbols)
    min_str  = config.signals.get("min_strength", 30)

    if not symbols:
        logger.error("自选股列表为空，退出")
        return
    if not chat_ids:
        logger.error("无推送目标 chat_id，退出")
        return

    finnhub_key    = config.FINNHUB_API_KEY
    anthropic_key  = config.ANTHROPIC_API_KEY
    finnhub_client = FinnhubClient(finnhub_key) if finnhub_key else None
    use_ai         = bool(anthropic_key)

    logger.info("开始扫描 %d 只股票（AI分析: %s）→ 推送到 %d 个 chat",
                len(symbols), "开启" if use_ai else "关闭", len(chat_ids))

    client = YFinanceDataClient()
    bot    = Bot(token=config.TELEGRAM_BOT_TOKEN)

    macro_context = _get_macro_context(finnhub_client)

    # ── Serenity 板块观点 ─────────────────────────
    try:
        picks = get_serenity_picks()
        serenity_text = format_serenity_section(picks)
        if serenity_text:
            for chat_id in chat_ids:
                await bot.send_message(chat_id=chat_id, text=serenity_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning("Serenity 获取失败: %s", e)

    # ── 逐只扫描 ──────────────────────────────────
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

            # ── 发送原有技术分析报告 ──
            text = format_signal_message(result, pinned=is_pinned)
            for chat_id in chat_ids:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)

            # ── 发送 AI 深度研判（紧接技术报告之后）──
            if use_ai:
                try:
                    ai_result = run_ai_analysis(
                        result, finnhub_client, anthropic_key, macro_context
                    )
                    ai_text = format_ai_analysis(sym, result.current_price, ai_result)
                    if ai_text:
                        await asyncio.sleep(0.3)
                        for chat_id in chat_ids:
                            await bot.send_message(
                                chat_id=chat_id, text=ai_text, parse_mode=ParseMode.HTML
                            )
                except Exception as e:
                    logger.error("AI 分析推送失败 %s: %s", sym, e)

            logger.info("✅ 推送 %s: %s 强度%d", sym, result.direction, result.strength)
            pushed += 1
            await asyncio.sleep(0.8)
        except Exception as e:
            logger.error("处理 %s 失败: %s", sym, e)

    logger.info("扫描完成，共推送 %d 条信号", pushed)


if __name__ == "__main__":
    asyncio.run(main())
