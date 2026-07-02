"""
Telegram Bot：命令处理 + Application 构建。
调度器通过 post_init 钩子在同一个 asyncio 事件循环中启动。
"""
import logging
import asyncio
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

from src.notifications.formatter import (
    format_watchlist_message,
    format_help_message,
    format_signal_message,
    format_deep_report,
)
from src.storage import watchlist_repo

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# 命令处理器
# ------------------------------------------------------------------ #

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    watchlist_repo.add_subscriber(chat_id)
    await update.message.reply_text(format_help_message(), parse_mode=ParseMode.HTML)


async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    symbols = watchlist_repo.get_all_symbols()
    await update.message.reply_text(
        format_watchlist_message(symbols), parse_mode=ParseMode.HTML
    )


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("用法：/add AAPL")
        return
    symbol = ctx.args[0].upper().strip()
    ok = watchlist_repo.add_symbol(symbol)
    if ok:
        await update.message.reply_text(f"✅ 已添加 <b>{symbol}</b>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ {symbol} 已在自选股列表中")


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("用法：/remove AAPL")
        return
    symbol = ctx.args[0].upper().strip()
    ok = watchlist_repo.remove_symbol(symbol)
    if ok:
        await update.message.reply_text(f"🗑️ 已移除 <b>{symbol}</b>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ 未找到 {symbol}")


async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("用法：/analyze AAPL")
        return
    symbol = ctx.args[0].upper().strip()
    await update.message.reply_text(f"⏳ 正在分析 <b>{symbol}</b>，请稍候...", parse_mode=ParseMode.HTML)

    data_client = ctx.bot_data.get("data_client")
    config = ctx.bot_data.get("config")
    if data_client is None:
        await update.message.reply_text("❌ 数据客户端未初始化")
        return

    from src.analysis.multi_timeframe import analyze_symbol
    result = await asyncio.get_event_loop().run_in_executor(
        None, analyze_symbol, data_client, symbol,
        config.signals.get("lookback_days", 250),
        config.signals.get("lookback_weeks", 104),
    )
    text = format_signal_message(result)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_deep(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """深度报告：财报历史、收入拆分、分红、股东结构、机构持仓。"""
    if not ctx.args:
        await update.message.reply_text("用法：/deep NVDA")
        return
    symbol = ctx.args[0].upper().strip()
    await update.message.reply_text(
        f"⏳ 正在生成 <b>{symbol}</b> 深度报告，约需 15 秒...", parse_mode=ParseMode.HTML
    )

    data_client = ctx.bot_data.get("data_client")
    config = ctx.bot_data.get("config")
    if data_client is None:
        await update.message.reply_text("❌ 数据客户端未初始化")
        return

    from src.analysis.multi_timeframe import analyze_symbol

    def _run():
        signal = analyze_symbol(
            data_client, symbol,
            config.signals.get("lookback_days", 250),
            config.signals.get("lookback_weeks", 104),
            config.total_capital, config.max_position_pct,
        )
        deep = data_client.get_deep_report(symbol)
        return signal, deep

    signal, deep = await asyncio.get_event_loop().run_in_executor(None, _run)
    text = format_deep_report(symbol, signal, deep)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """手动触发完整扫描，等效于每日 21:00 自动任务。"""
    await update.message.reply_text("⏳ 开始扫描自选股，约需 1-2 分钟...")
    from src.scheduler import _daily_scan_job_impl
    try:
        await _daily_scan_job_impl(ctx.application)
        await update.message.reply_text("✅ 扫描完成")
    except Exception as exc:
        await update.message.reply_text(f"❌ 扫描出错: {exc}")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    symbols = watchlist_repo.get_all_symbols()
    chat_ids = watchlist_repo.get_all_chat_ids()

    next_run = "未知"
    jobs = ctx.job_queue.jobs() if ctx.job_queue else []
    scan_jobs = [j for j in jobs if j.name == "daily_scan"]
    if scan_jobs:
        nrt = scan_jobs[0].next_t
        if nrt:
            next_run = nrt.strftime("%Y-%m-%d %H:%M %Z")

    text = (
        f"<b>📊 系统状态</b>\n\n"
        f"自选股数量: {len(symbols)} 只\n"
        f"订阅用户数: {len(chat_ids)}\n"
        f"下次扫描: {next_run}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------ #
# Application 构建
# ------------------------------------------------------------------ #

def build_application(config) -> Application:
    from src.scheduler import setup_scheduler

    async def post_init(app: Application) -> None:
        setup_scheduler(app, config)
        logger.info("定时任务已注册（JobQueue）")

    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("status", cmd_status))

    return app
