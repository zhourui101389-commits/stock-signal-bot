"""
定时任务：
- 21:00（北京时间，工作日）：完整扫描自选股，强度≥30 发完整信号（含今日涨跌预估）
- 22:00 / 23:00 / 00:00：对 21:00 已触发的股票做追踪推送（实时涨跌 vs 预估、买卖盘比）
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta, time

import pytz
from telegram import Bot
from telegram.ext import Application, ContextTypes
from telegram.constants import ParseMode

from src.storage import watchlist_repo
from src.analysis.multi_timeframe import analyze_symbol
from src.notifications.push import push_signal
from src.notifications.formatter import format_followup_message, format_economic_calendar, format_serenity_section
from src.data.economic_calendar import get_us_events
from src.data.serenity_tracker import get_serenity_picks

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
_FLAGGED_KEY = "flagged_today"   # app.bot_data 里存当日已触发股票的 key
_FLAGGED_DATE_KEY = "flagged_date"


# ──────────────────────────────────────────────
# 21:00 完整扫描
# ──────────────────────────────────────────────

async def daily_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _daily_scan_job_impl(context.application)
    except Exception as exc:
        logger.exception("【21:00】扫描任务顶层异常: %s", exc)


async def _daily_scan_job_impl(app: Application) -> None:
    logger.info("【21:00】开始完整扫描...")
    data_client = app.bot_data.get("data_client")
    config = app.bot_data.get("config")
    if data_client is None:
        logger.error("data_client 未注册，跳过扫描")
        return

    # 每天重置当日追踪列表
    today = datetime.now(_CST).strftime("%Y-%m-%d")
    app.bot_data[_FLAGGED_KEY]      = {}
    app.bot_data[_FLAGGED_DATE_KEY] = today

    # ── 并发拉取宏观经济日历 + Serenity 供应链观点 ──────────────────────
    cal_cfg     = config.economic_calendar if config else {}
    finnhub_key = cal_cfg.get("finnhub_api_key", "") if cal_cfg else ""
    loop = asyncio.get_event_loop()

    try:
        if finnhub_key:
            eco_events, serenity_picks = await asyncio.gather(
                loop.run_in_executor(
                    None, get_us_events,
                    finnhub_key,
                    cal_cfg.get("days_before", 1),
                    cal_cfg.get("days_after",  3),
                ),
                loop.run_in_executor(None, get_serenity_picks),
            )
        else:
            logger.info("未配置 Finnhub API key，跳过经济日历")
            eco_events = []
            serenity_picks = await loop.run_in_executor(None, get_serenity_picks)
    except Exception as exc:
        logger.error("拉取日历/Serenity 异常: %s", exc)
        eco_events, serenity_picks = [], {}

    # 只存高影响事件（今日发布）供追踪更新引用
    app.bot_data["eco_events_today"] = [
        e for e in eco_events
        if e.get("time", "").startswith(today) and e.get("impact", "").lower() == "high"
    ]

    # 拼合日历消息 + Serenity 板块，一条消息发出
    cal_text      = format_economic_calendar(eco_events)
    serenity_text = format_serenity_section(serenity_picks)
    combined      = (cal_text + "\n" + serenity_text).strip() if (cal_text or serenity_text) else ""

    if combined:
        chat_ids = watchlist_repo.get_all_chat_ids()
        for chat_id in chat_ids:
            try:
                await app.bot.send_message(
                    chat_id=chat_id, text=combined, parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error("推送日历+Serenity 到 %s 失败: %s", chat_id, e)
        logger.info("日历+Serenity 已推送（事件 %d 个，Serenity source=%s）",
                    len(eco_events), serenity_picks.get("source", "?"))

    symbols = data_client.get_watchlist(group=config.moomoo_group if config else "US")
    if not symbols:
        logger.warning("Moomoo 自选股为空，fallback 本地列表")
        symbols = watchlist_repo.get_all_symbols()
    if not symbols:
        logger.info("自选股为空，跳过扫描")
        return

    min_strength  = config.signals.get("min_strength", 30) if config else 30
    lookback_days = config.signals.get("lookback_days", 250) if config else 250
    lookback_weeks = config.signals.get("lookback_weeks", 104) if config else 104
    total_capital  = config.total_capital if config else 10000
    max_position_pct = config.max_position_pct if config else 0.10
    pinned       = set(config.pinned_symbols)    if config else set()
    tier_core    = set(config.tier_core)          if config else set()
    tier_swing   = set(config.tier_swing)         if config else set()
    tier_spec    = set(config.tier_speculative)   if config else set()

    def _tier(sym: str) -> str:
        if sym in tier_core:  return "core"
        if sym in tier_spec:  return "speculative"
        if sym in tier_swing: return "swing"
        return "swing"   # 默认按机动仓处理

    logger.info("扫描 %d 只股票: %s", len(symbols), symbols)

    for i in range(0, len(symbols), 5):
        batch = symbols[i: i + 5]
        results = await asyncio.gather(*[
            asyncio.get_event_loop().run_in_executor(
                None, analyze_symbol, data_client, sym,
                lookback_days, lookback_weeks, total_capital, max_position_pct
            )
            for sym in batch
        ])
        for result in results:
            result.tier             = _tier(result.symbol)
            result.pinned           = result.symbol in pinned
            result._total_capital   = total_capital
            result._currency        = config.currency if config else "AUD"
            effective_min = 0 if result.pinned else min_strength
            pushed = await push_signal(app.bot, result, min_strength=effective_min,
                                       pinned=result.pinned)
            if pushed:
                # 记录到当日追踪列表，供后续整点更新
                app.bot_data[_FLAGGED_KEY][result.symbol] = {
                    "direction":              result.direction,
                    "strength":               result.strength,
                    "estimated_gain_pct":     result.estimated_gain_pct,
                    "estimated_gain_raw_pct": result.estimated_gain_raw_pct,
                    "initial_price":          result.current_price,
                    "atr_pct":                result.atr_pct,
                }
        if i + 5 < len(symbols):
            await asyncio.sleep(2)

    logger.info("【21:00】扫描完成，已追踪股票: %s", list(app.bot_data[_FLAGGED_KEY].keys()))


# ──────────────────────────────────────────────
# 22:00 / 23:00 / 00:00 追踪更新
# ──────────────────────────────────────────────

async def followup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _followup_job_impl(context.application)
    except Exception as exc:
        logger.exception("【追踪】任务顶层异常: %s", exc)


async def _followup_job_impl(app: Application) -> None:
    hour = datetime.now(_CST).hour
    logger.info("【%02d:00】开始追踪更新...", hour)

    data_client = app.bot_data.get("data_client")
    if data_client is None:
        return

    flagged: dict = app.bot_data.get(_FLAGGED_KEY, {})
    if not flagged:
        logger.info("当日无已追踪股票，跳过")
        return

    # 并发拉取实时报价 + 今日资金流
    async def _fetch_one(sym: str, orig: dict) -> dict | None:
        loop = asyncio.get_event_loop()
        try:
            quote, flow = await asyncio.gather(
                loop.run_in_executor(None, data_client.get_quote, sym),
                loop.run_in_executor(None, data_client.get_capital_flow_summary, sym),
            )
            return {**orig, "symbol": sym, "quote": quote, "flow": flow}
        except Exception as e:
            logger.error("拉取 %s 实时数据失败: %s", sym, e)
            return None

    tasks = [_fetch_one(sym, orig) for sym, orig in flagged.items()]
    results = await asyncio.gather(*tasks)
    updates = [r for r in results if r is not None]

    if not updates:
        return

    # 附上今日已公布的高影响力宏观事件摘要
    released = [
        e for e in app.bot_data.get("eco_events_today", [])
        if e.get("actual") is not None
    ]
    eco_note = ""
    if released:
        parts = []
        for e in released[:3]:
            name    = e.get("cn_name") or e.get("event", "")[:20]
            actual  = e.get("actual")
            est     = e.get("estimate")
            unit    = e.get("unit", "")

            def _fv(v):
                if v is None: return "—"
                try:
                    f = float(v)
                    u = (unit or "").strip()
                    return (f"{f:+.2f}%" if u == "%" else
                            f"{f:.1f}{u}" if u in ("K","M","B") else f"{f:+.2f}")
                except Exception:
                    return str(v)

            beat = ""
            if actual is not None and est is not None:
                try:
                    beat = " ✅" if float(actual) > float(est) else " ⚠️"
                except Exception:
                    pass
            parts.append(f"  • {name}: 实际 <b>{_fv(actual)}</b> vs 预估 {_fv(est)}{beat}")
        eco_note = "\n\n🔔 <b>今日已公布宏观数据</b>\n" + "\n".join(parts)

    text = format_followup_message(hour, updates)
    if not text:
        return
    if eco_note:
        text = text + eco_note

    chat_ids = watchlist_repo.get_all_chat_ids()
    for chat_id in chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            logger.info("追踪更新已推送到 chat_id=%s", chat_id)
        except Exception as e:
            logger.error("推送追踪更新到 %s 失败: %s", chat_id, e)


# ──────────────────────────────────────────────
# 调度器注册（使用 ptb JobQueue，与 bot event loop 共享，无兼容性问题）
# ──────────────────────────────────────────────

def _maybe_schedule_recovery(app: Application, tz) -> None:
    """
    启动时补跑检测：bot 启动/唤醒时，只要今天是工作日且21:00已过、扫描未运行，
    立即补跑。不限制唤醒时间——覆盖 Mac 整晚睡眠后次日早晨才醒来的场景。
    """
    from datetime import datetime as _dt
    now     = _dt.now(tz)
    weekday = now.weekday()   # 0=Mon … 4=Fri
    hour    = now.hour

    # 工作日 且 21:00 已过（包含 22/23 点及跨午夜的 0-8 点）
    scan_window_passed = (weekday < 5 and hour >= 21) or (weekday < 5 and hour < 9)
    if not scan_window_passed:
        return

    # 确定"应扫描日期"：0-8 点属于前一天的扫描窗口
    from datetime import timedelta as _td
    scan_date = (now - _td(hours=9)).strftime("%Y-%m-%d") if hour < 9 else now.strftime("%Y-%m-%d")
    flagged_date = app.bot_data.get(_FLAGGED_DATE_KEY, "")
    if flagged_date != scan_date:
        logger.warning("启动补跑：%s 21:00 扫描未运行（当前 %02d:%02d），60 秒后补跑",
                       scan_date, hour, now.minute)
        app.job_queue.run_once(daily_scan_job, when=60, name="startup_recovery")


def setup_scheduler(app: Application, config) -> None:
    tz_str = config.scheduler.get("timezone", "Asia/Shanghai")
    tz     = pytz.timezone(tz_str)
    jq     = app.job_queue

    # 21:00 完整分析（工作日）
    jq.run_daily(
        daily_scan_job,
        time=time(21, 0, 0, tzinfo=tz),
        days=(0, 1, 2, 3, 4),    # Mon-Fri
        name="daily_scan",
    )

    # 22:00 / 23:00 / 00:00 追踪更新（工作日）
    for h in (22, 23, 0):
        jq.run_daily(
            followup_job,
            time=time(h, 0, 0, tzinfo=tz),
            days=(0, 1, 2, 3, 4),
            name=f"followup_{h:02d}",
        )

    # misfire_grace_time=7200：直接修改 APScheduler 底层 job
    # PTB 的 job_kwargs 不透传此参数，必须在注册后单独设置
    try:
        for aps_job in jq.scheduler.get_jobs():
            aps_job.modify(misfire_grace_time=7200)
        logger.info("APScheduler misfire_grace_time 已设为 7200 秒（共 %d 个任务）",
                    len(jq.scheduler.get_jobs()))
    except Exception as e:
        logger.warning("设置 misfire_grace_time 失败: %s", e)

    # 启动时补跑检测（launchd 重启场景）
    _maybe_schedule_recovery(app, tz)

    logger.info(
        "定时任务已注册（JobQueue）: 21:00 扫描 + 22/23/00:00 追踪 (%s)，misfire_grace=2h",
        tz_str,
    )
