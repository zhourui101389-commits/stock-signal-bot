"""
云端扫描脚本（GitHub Actions 调用）。
每天 21:00 CST 自动运行，完成后退出。
依赖环境变量：TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, FINNHUB_API_KEY, ANTHROPIC_API_KEY
"""
import asyncio
import datetime
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
from src.storage.prediction_repo import (
    load_predictions, save_predictions, get_symbol_history,
)
from src.data.yfinance_client import YFinanceDataClient
from src.data.finnhub_client import FinnhubClient
from src.analysis.multi_timeframe import analyze_symbol
from src.analysis.ai_analyst import run_ai_analysis
from src.notifications.formatter import (
    format_signal_message,
    format_review_message,
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


async def _run_review(bot: Bot, chat_ids: list[int]) -> None:
    """加载昨日预测，对比实际收盘价，发送复盘消息。"""
    import yfinance as yf

    data = load_predictions()
    if not data:
        logger.info("无昨日预测记录，跳过复盘")
        return

    scan_date = data.get("scan_date", "")
    predictions = data.get("predictions", [])
    if not predictions:
        return

    logger.info("开始复盘 %s，共 %d 条预测", scan_date, len(predictions))

    reviewed = []
    for pred in predictions:
        sym         = pred["symbol"]
        final_dir   = pred.get("final_direction", "中性")
        entry_price = pred.get("entry_price")
        target      = pred.get("target_price")
        stop        = pred.get("stop_loss")

        # 拉取扫描日的实际收盘价（从扫描日起最多取3天，应对周末/假期）
        close_price = None
        try:
            start = datetime.date.fromisoformat(scan_date)
            end   = start + datetime.timedelta(days=4)
            hist  = yf.Ticker(sym).history(start=str(start), end=str(end))
            if not hist.empty:
                close_price = float(hist["Close"].iloc[0])
        except Exception as e:
            logger.warning("复盘拉取 %s 收盘价失败: %s", sym, e)

        actual_pct = None
        correct    = None
        hit_target = False
        hit_stop   = False

        if close_price and entry_price:
            actual_pct = (close_price - entry_price) / entry_price * 100
            if final_dir == "看多":
                correct = actual_pct > 0
                if target:
                    hit_target = close_price >= target
                if stop:
                    hit_stop = close_price <= stop
            elif final_dir == "看空":
                correct = actual_pct < 0
                if stop:
                    hit_stop = close_price >= stop
            # 中性：correct=None，不计入胜率

        reviewed.append({
            **pred,
            "close_price": close_price,
            "actual_pct":  actual_pct,
            "correct":     correct,
            "hit_target":  hit_target,
            "hit_stop":    hit_stop,
        })

    msg = format_review_message(scan_date, reviewed)
    if msg:
        for chat_id in chat_ids:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        logger.info("复盘消息已发送")


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

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

    # ── 先发昨日复盘 ──────────────────────────────────
    try:
        await _run_review(bot, chat_ids)
    except Exception as e:
        logger.error("复盘失败: %s", e)

    logger.info("开始扫描 %d 只股票（AI分析: %s）→ 推送到 %d 个 chat",
                len(symbols), "开启" if use_ai else "关闭", len(chat_ids))

    client = YFinanceDataClient()
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

    # ── 逐只扫描，同时收集今日预测 ──────────────────
    pushed = 0
    today_predictions = []

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

            # ── AI 综合研判 ──
            ai_result = {}
            if use_ai:
                try:
                    # 把昨日预测历史传给 AI 参考
                    symbol_history = get_symbol_history(sym)
                    ai_result = run_ai_analysis(
                        result, finnhub_client, anthropic_key, macro_context,
                        symbol_history=symbol_history,
                    )
                    logger.info("AI综合研判 %s: %s 置信度%s",
                                sym, ai_result.get("final_direction", "?"),
                                ai_result.get("conviction", "?"))
                except Exception as e:
                    logger.error("AI 分析失败 %s: %s", sym, e)

            # ── 发送消息 ──
            price = result.current_price
            msg = format_signal_message(result, pinned=is_pinned, ai_result=ai_result or None)
            for chat_id in chat_ids:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

            logger.info("✅ 推送 %s: %s 强度%d", sym, result.direction, result.strength)
            pushed += 1

            # ── 记录今日预测 ──
            today_predictions.append({
                "symbol":          sym,
                "direction":       result.direction,
                "final_direction": ai_result.get("final_direction", "中性") if ai_result else
                                   {"BUY": "看多", "SELL": "看空"}.get(result.direction, "中性"),
                "action":          ai_result.get("action", "") if ai_result else "",
                "conviction":      ai_result.get("conviction", "") if ai_result else "",
                "entry_price":     round(price, 4) if price else None,
                "target_price":    ai_result.get("target_price") if ai_result else None,
                "stop_loss":       ai_result.get("stop_loss") if ai_result else None,
                "verdict":         ai_result.get("verdict", "") if ai_result else "",
            })

            await asyncio.sleep(0.8)
        except Exception as e:
            logger.error("处理 %s 失败: %s", sym, e)

    logger.info("扫描完成，共推送 %d 条信号", pushed)

    # ── 保存今日预测供明天复盘 ───────────────────────
    if today_predictions:
        save_predictions(today_predictions)


if __name__ == "__main__":
    asyncio.run(main())
