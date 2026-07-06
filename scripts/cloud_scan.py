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


def _is_us_trading_day() -> bool:
    """判断今天是否为美股交易日（排除周末和节假日）。盘前 9AM EDT 调用。"""
    import yfinance as yf
    today = datetime.date.today()
    if today.weekday() >= 5:  # 周六/周日
        return False
    try:
        hist = yf.download("SPY", period="1d", interval="1m",
                           prepost=True, progress=False)
        if hist.empty:
            return False
        last_date = hist.index[-1].date()
        return last_date == today
    except Exception:
        return True  # 网络异常时默认继续


def _get_macro_context(finnhub: FinnhubClient | None) -> str:
    import yfinance as yf
    parts = []

    # 大盘指数 + VIX
    try:
        tickers = yf.download(["SPY", "QQQ", "^VIX"], period="5d",
                              progress=False, auto_adjust=True)
        close = tickers["Close"]

        def _chg(sym, days=1):
            col = close[sym].dropna()
            if len(col) > days:
                return (col.iloc[-1] / col.iloc[-1 - days] - 1) * 100
            return None

        spy_d  = _chg("SPY", 1)
        spy_5d = _chg("SPY", min(4, len(close["SPY"].dropna()) - 1))
        qqq_d  = _chg("QQQ", 1)
        vix    = close["^VIX"].dropna().iloc[-1] if not close["^VIX"].dropna().empty else None

        if spy_d is not None:
            parts.append(f"SPY: {spy_d:+.2f}%（今日）/ {spy_5d:+.2f}%（5日）")
        if qqq_d is not None:
            parts.append(f"QQQ: {qqq_d:+.2f}%（今日）")
        if vix is not None:
            vix_label = "极度恐慌" if vix > 35 else ("恐慌" if vix > 25 else ("偏高" if vix > 18 else "平稳"))
            parts.append(f"VIX: {vix:.1f}（{vix_label}）")
    except Exception as e:
        logger.debug("宏观指标获取失败: %s", e)

    # 市场新闻
    if finnhub:
        try:
            news = finnhub.get_market_news("general")
            if news:
                headlines = [f"- {n['headline']}" for n in news[:5]]
                parts.append("近期市场新闻：\n" + "\n".join(headlines))
        except Exception:
            pass

    return "\n".join(parts) if parts else "宏观数据暂不可用"


def _ai_review_analysis(reviewed: list[dict], scan_date: str, anthropic_key: str) -> str:
    """调用 Claude Haiku 对复盘结果做模式分析，输出改进方向。"""
    import anthropic

    right = [r for r in reviewed if r.get("correct") is True]
    wrong = [r for r in reviewed if r.get("correct") is False]
    if not right and not wrong:
        return ""

    def _fmt(r: dict) -> str:
        apct = r.get("actual_pct")
        return (
            f"- {r['symbol']}：预判{r.get('final_direction','?')}"
            f"（置信度{r.get('conviction','')}），"
            f"实际{f'{apct:+.2f}%' if apct is not None else '无数据'}。"
            f"依据：{r.get('verdict','')}"
        )

    prompt = (
        f"量化交易复盘分析，日期 {scan_date}：\n\n"
        f"✅ 方向正确（{len(right)}只）：\n" + "\n".join(_fmt(r) for r in right) + "\n\n"
        f"❌ 方向错误（{len(wrong)}只）：\n" + "\n".join(_fmt(r) for r in wrong) + "\n\n"
        f"用100字以内分析：①错误预测的共同特征或原因；②今日扫描应重点注意什么。"
        f"直接给出分析，不用列序号。"
    )
    try:
        client = anthropic.Anthropic(api_key=anthropic_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.warning("AI复盘分析失败: %s", e)
        return ""


async def _run_review(bot: Bot, chat_ids: list[int], anthropic_key: str = "") -> None:
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

        close_price = None
        try:
            start = datetime.date.fromisoformat(scan_date)
            end   = start + datetime.timedelta(days=4)
            hist  = yf.Ticker(sym).history(start=str(start), end=str(end))
            if not hist.empty:
                close_price = float(hist["Close"].iloc[0])
        except Exception as e:
            logger.warning("复盘拉取 %s 收盘价失败: %s", sym, e)

        actual_pct   = None
        correct      = None
        inconclusive = False
        hit_target   = False
        hit_stop     = False

        if close_price and entry_price:
            actual_pct = (close_price - entry_price) / entry_price * 100
            if abs(actual_pct) < 0.5:
                # 涨跌幅过小（节假日/半天市场/数据误差），不计入对错
                inconclusive = True
            elif final_dir == "看多":
                correct = actual_pct > 0
                if target:
                    hit_target = close_price >= target
                if stop:
                    hit_stop = close_price <= stop
            elif final_dir == "看空":
                correct = actual_pct < 0
                if stop:
                    hit_stop = close_price >= stop
            # 中性：correct=None

        reviewed.append({
            **pred,
            "close_price":  close_price,
            "actual_pct":   actual_pct,
            "correct":      correct,
            "inconclusive": inconclusive,
            "hit_target":   hit_target,
            "hit_stop":     hit_stop,
        })

    save_predictions(reviewed, scan_date=scan_date)
    logger.info("复盘结果（含正确/错误标记）已写回预测文件")

    ai_analysis = ""
    if anthropic_key:
        ai_analysis = _ai_review_analysis(reviewed, scan_date, anthropic_key)

    msg = format_review_message(scan_date, reviewed, ai_analysis=ai_analysis)
    if msg:
        for chat_id in chat_ids:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        logger.info("复盘消息已发送")


async def _run_scan(config, bot, chat_ids, finnhub_client, anthropic_key):
    """盘前扫描：分析信号、调用 AI、推送 Telegram、保存今日预测。"""
    if not _is_us_trading_day():
        logger.info("今日非美股交易日（节假日/周末），跳过盘前扫描")
        return

    symbols = watchlist_repo.get_all_symbols()
    pinned  = set(config.pinned_symbols)
    min_str = config.signals.get("min_strength", 30)
    use_ai  = bool(anthropic_key)

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

            ai_result = {}
            if use_ai:
                try:
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

            price = result.current_price
            msg = format_signal_message(result, pinned=is_pinned, ai_result=ai_result or None)
            for chat_id in chat_ids:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

            logger.info("✅ 推送 %s: %s 强度%d", sym, result.direction, result.strength)
            pushed += 1

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

    if today_predictions:
        save_predictions(today_predictions)


async def main():
    # SCAN_MODE: "scan"（仅扫描）| "review"（仅复盘）| 不设（两者都跑，向后兼容）
    scan_mode = os.environ.get("SCAN_MODE", "").lower()

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

    chat_ids = _get_chat_ids(config)
    if not watchlist_repo.get_all_symbols():
        logger.error("自选股列表为空，退出")
        return
    if not chat_ids:
        logger.error("无推送目标 chat_id，退出")
        return

    finnhub_key    = config.FINNHUB_API_KEY
    anthropic_key  = config.ANTHROPIC_API_KEY
    finnhub_client = FinnhubClient(finnhub_key) if finnhub_key else None

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

    if scan_mode == "review":
        try:
            await _run_review(bot, chat_ids, anthropic_key)
        except Exception as e:
            logger.error("复盘失败: %s", e)

    elif scan_mode == "scan":
        await _run_scan(config, bot, chat_ids, finnhub_client, anthropic_key)

    else:
        try:
            await _run_review(bot, chat_ids, anthropic_key)
        except Exception as e:
            logger.error("复盘失败: %s", e)
        await _run_scan(config, bot, chat_ids, finnhub_client, anthropic_key)


if __name__ == "__main__":
    asyncio.run(main())
