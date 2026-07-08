"""
云端扫描脚本（GitHub Actions 调用）。
每天 21:00 CST 自动运行，完成后退出。
依赖环境变量：TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, FINNHUB_API_KEY, ANTHROPIC_API_KEY
"""
import asyncio
import datetime
import html as _html
import logging
import os
import sys
import warnings
warnings.filterwarnings("ignore")

_TG_MAX_CHARS = 4096

# 相关性高的板块集群：同日 ≥2 只看多时触发风险提示
_CORR_CLUSTERS = {
    "半导体": {"NVDA", "AMD", "SOXL", "MU", "AVGO", "QCOM", "TSM", "INTC", "SMCI", "ARM", "AMAT", "ASML", "SOXX"},
    "科技巨头": {"AAPL", "MSFT", "GOOGL", "GOOG", "META", "AMZN"},
    "AI基础设施": {"NVDA", "MSFT", "ORCL", "AMZN", "GOOGL", "IONQ", "PLTR"},
    "电动车": {"TSLA", "NIO", "RIVN", "LCID", "LI", "XPEV"},
    "中概": {"BABA", "JD", "PDD", "BIDU", "TME", "KWEB"},
    "黄金/大宗": {"GLD", "SLV", "GDX", "GOLD", "NEM"},
    "生物医药": {"XBI", "IBB", "MRNA", "PFE", "ABBV", "GILD"},
}

# ── Alpaca 执行风控参数 ──────────────────────────────────────
_MAX_LOSS_PER_TRADE_PCT = 0.005   # 单笔最大亏损占总资产比例（按入场-止损反推仓位）
_MAX_TOTAL_EXPOSURE_PCT = 0.60    # 最大总仓位敞口（占总资产）
_MAX_POSITIONS          = 8       # 最多同时持仓数
_MIN_WINRATE_SAMPLES    = 8       # 胜率门禁生效所需的最小历史样本数
_MIN_WINRATE            = 0.45    # 低于此历史胜率的 action 暂停自动执行

_SCREENER_TOP_N = 15   # 市场初筛（标普500+中概）每日入选候选数

def _safe_truncate(msg: str, limit: int = _TG_MAX_CHARS) -> str:
    """Telegram 消息超过限制时截断并加提示，避免发送失败。"""
    if len(msg) <= limit:
        return msg
    cutoff = limit - 60
    return msg[:cutoff] + "\n\n<i>⚠️ 消息过长，已截断，请用 /deep 查看完整报告</i>"

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from telegram import Bot
from telegram.constants import ParseMode

from src.config import Config
from src.storage.database import init_db
from src.storage import watchlist_repo
from src.storage.prediction_repo import (
    load_predictions, save_predictions, save_raw, get_symbol_history,
)
from src.data.yfinance_client import YFinanceDataClient
from src.data.finnhub_client import FinnhubClient
from src.analysis.multi_timeframe import analyze_symbol
from src.analysis.ai_analyst import run_ai_analysis
from src.analysis.screener import screen_top_candidates
from src.notifications.formatter import (
    format_signal_message,
    format_review_message,
    format_weekly_report,
)

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
    """判断今天是否为美股交易日（排除周末和节假日）。按美东时区判断，避免 UTC 运行环境跨天误判。"""
    import yfinance as yf
    from zoneinfo import ZoneInfo
    today = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    if today.weekday() >= 5:  # 周六/周日
        return False
    try:
        hist = yf.download("SPY", period="1d", interval="1m",
                           prepost=True, progress=False)
        if hist.empty:
            return False
        last_date = hist.index[-1].tz_convert("America/New_York").date()
        return last_date == today
    except Exception:
        return True  # 网络异常时默认继续


def _get_nth_trading_close(sym: str, scan_date_str: str, n: int):
    """返回 (close_price, date_str) — scan_date 之后第 n 个交易日的收盘价。"""
    import yfinance as yf
    today = datetime.date.today()
    start = datetime.date.fromisoformat(scan_date_str) + datetime.timedelta(days=1)
    if start > today:
        return None, None
    end = min(start + datetime.timedelta(days=n * 2 + 7), today + datetime.timedelta(days=1))
    try:
        hist = yf.Ticker(sym).history(start=str(start), end=str(end))
        if len(hist) >= n:
            return float(hist["Close"].iloc[n - 1]), hist.index[n - 1].date().isoformat()
    except Exception:
        pass
    return None, None


def _fill_multi_day_outcomes() -> bool:
    """扫描 history 中未完成的 T+1/T+3/T+5 复盘结果及退出追踪，批量下载 OHLC 后原地写回。"""
    import yfinance as yf
    path = os.environ.get("PREDICTIONS_FILE", "/tmp/predictions.json")
    data = load_predictions(path)
    if not data:
        return False

    today   = datetime.date.today()
    updated = False

    # 按 scan_date 分组，收集需要补全的请求
    needed: dict[str, dict[str, list[int]]] = {}
    for day in data.get("history", []):
        scan_date = day.get("scan_date", "")
        if not scan_date:
            continue
        scan_d = datetime.date.fromisoformat(scan_date)
        for pred in day.get("predictions", []):
            sym   = pred.get("symbol", "")
            entry = pred.get("entry_price")
            if not sym or not entry:
                continue
            for n, key_c, _, _ in [
                (1, "t1_close", "t1_pct", "t1_correct"),
                (3, "t3_close", "t3_pct", "t3_correct"),
                (5, "t5_close", "t5_pct", "t5_correct"),
            ]:
                if pred.get(key_c) is not None:
                    continue
                if today < scan_d + datetime.timedelta(days=n):
                    continue
                needed.setdefault(scan_date, {}).setdefault(sym, [])
                if n not in needed[scan_date][sym]:
                    needed[scan_date][sym].append(n)
            # 退出追踪：按 AI 给出的持仓周期动态确定退出窗口
            h_str_n     = pred.get("horizon", "3-5天")
            ew_n        = 20 if "2-4周" in h_str_n else (10 if "1-2周" in h_str_n or "1周" in h_str_n else 5)
            if not pred.get("exit_tracked"):
                # 日历日 buffer：trading day ×1.5 + 3（覆盖长假期，如感恩节周）
                cal_buf = int(ew_n * 1.5) + 3
                if today >= scan_d + datetime.timedelta(days=cal_buf):
                    needed.setdefault(scan_date, {}).setdefault(sym, [])
                    if ew_n not in needed[scan_date][sym]:
                        needed[scan_date][sym].append(ew_n)
            # T+10 / T+20（长周期 horizon 额外跟踪点，早于 exit_window 触发）
            for extra_n in [10, 20]:
                if extra_n > ew_n or pred.get(f"t{extra_n}_close") is not None:
                    continue
                if today >= scan_d + datetime.timedelta(days=extra_n):
                    needed.setdefault(scan_date, {}).setdefault(sym, [])
                    if extra_n not in needed[scan_date][sym]:
                        needed[scan_date][sym].append(extra_n)
            # 出局后5日（exit_tracked 完成后，检验出局时机）
            if pred.get("exit_tracked") and pred.get("post_exit_5d_pct") is None:
                post_n   = ew_n + 5
                cal_post = int(post_n * 1.5) + 3
                if today >= scan_d + datetime.timedelta(days=cal_post):
                    needed.setdefault(scan_date, {}).setdefault(sym, [])
                    if post_n not in needed[scan_date][sym]:
                        needed[scan_date][sym].append(post_n)

    if not needed:
        return False

    # 批量下载 OHLC（Close / High / Low）
    price_cache: dict[str, dict] = {}
    for scan_date, sym_ns in needed.items():
        scan_d  = datetime.date.fromisoformat(scan_date)
        max_n   = max(n for ns in sym_ns.values() for n in ns)
        start   = str(scan_d + datetime.timedelta(days=1))
        end     = str(scan_d + datetime.timedelta(days=max_n * 2 + 10))
        symbols = list(sym_ns.keys())
        try:
            df = yf.download(symbols, start=start, end=end,
                             progress=False, auto_adjust=True)

            def _col(name: str):
                raw = df[name]
                if len(symbols) > 1:
                    return raw
                return raw.rename(symbols[0]).to_frame()

            close_df = _col("Close")
            high_df  = _col("High")
            low_df   = _col("Low")

            price_cache[scan_date] = {
                sym: {
                    "close": close_df[sym].dropna(),
                    "high":  high_df[sym].dropna() if sym in high_df.columns else None,
                    "low":   low_df[sym].dropna() if sym in low_df.columns else None,
                }
                for sym in symbols if sym in close_df.columns
            }
        except Exception as e:
            logger.warning("批量下载 %s 价格失败: %s", scan_date, e)

    # 写回结果
    for day in data.get("history", []):
        scan_date = day.get("scan_date", "")
        if scan_date not in price_cache:
            continue
        sym_data = price_cache[scan_date]

        for pred in day.get("predictions", []):
            sym       = pred.get("symbol", "")
            final_dir = pred.get("final_direction", "中性")
            entry     = pred.get("entry_price")
            if not sym or not entry or sym not in sym_data:
                continue

            series = sym_data[sym]["close"]
            high_s = sym_data[sym].get("high")
            low_s  = sym_data[sym].get("low")
            h_str       = pred.get("horizon", "3-5天")
            exit_window = 20 if "2-4周" in h_str else (10 if "1-2周" in h_str or "1周" in h_str else 5)

            # ── T+1 / T+3 / T+5 收盘价 ───────────────────────────────
            for n, key_c, key_p, key_r in [
                (1, "t1_close", "t1_pct", "t1_correct"),
                (3, "t3_close", "t3_pct", "t3_correct"),
                (5, "t5_close", "t5_pct", "t5_correct"),
            ]:
                if pred.get(key_c) is not None:
                    continue
                if len(series) < n:
                    continue
                c   = float(series.iloc[n - 1])
                pct = (c - entry) / entry * 100
                cor = (
                    None if abs(pct) < 0.3
                    else (pct > 0 if final_dir == "看多" else (pct < 0 if final_dir == "看空" else None))
                )
                pred[key_c] = round(c, 4)
                pred[key_p] = round(pct, 4)
                pred[key_r] = cor
                updated = True

            # ── T+10 / T+20（1-2周 / 2-4周 horizon 额外跟踪点）─────────
            for n, key_c, key_p, key_r in [
                (10, "t10_close", "t10_pct", "t10_correct"),
                (20, "t20_close", "t20_pct", "t20_correct"),
            ]:
                if n > exit_window or pred.get(key_c) is not None or len(series) < n:
                    continue
                c   = float(series.iloc[n - 1])
                pct = (c - entry) / entry * 100
                cor = (
                    None if abs(pct) < 0.3
                    else (pct > 0 if final_dir == "看多" else (pct < 0 if final_dir == "看空" else None))
                )
                pred[key_c] = round(c, 4)
                pred[key_p] = round(pct, 4)
                pred[key_r] = cor
                updated = True

            # ── 退出追踪（按 horizon 动态窗口，一次性计算）──────────────

            if (not pred.get("exit_tracked")
                    and len(series) >= exit_window
                    and high_s is not None and low_s is not None
                    and len(high_s) >= exit_window and len(low_s) >= exit_window):

                target_price = pred.get("target_price")
                stop_loss    = pred.get("stop_loss")
                n_days       = min(exit_window, len(high_s), len(low_s))
                highs        = [float(high_s.iloc[i]) for i in range(n_days)]
                lows         = [float(low_s.iloc[i]) for i in range(n_days)]

                peak       = max(highs)
                trough     = min(lows)
                peak_day   = highs.index(peak) + 1
                trough_day = lows.index(trough) + 1

                pred["holding_peak_pct"]   = round((peak   - entry) / entry * 100, 4)
                pred["holding_trough_pct"] = round((trough - entry) / entry * 100, 4)
                pred["holding_peak_day"]   = peak_day
                pred["holding_trough_day"] = trough_day

                # 止盈/止损首次触达日
                target_hit_day = stop_hit_day = None
                for i in range(n_days):
                    h, l = highs[i], lows[i]
                    if target_price and target_hit_day is None:
                        if final_dir == "看多" and h >= target_price:
                            target_hit_day = i + 1
                        elif final_dir == "看空" and l <= target_price:
                            target_hit_day = i + 1
                    if stop_loss and stop_hit_day is None:
                        if final_dir == "看多" and l <= stop_loss:
                            stop_hit_day = i + 1
                        elif final_dir == "看空" and h >= stop_loss:
                            stop_hit_day = i + 1

                pred["target_hit_day"] = target_hit_day
                pred["stop_hit_day"]   = stop_hit_day

                # 有效退出：止盈优先，其次止损，最后持满 T+5
                if target_hit_day and (not stop_hit_day or target_hit_day <= stop_hit_day):
                    exit_p, exit_r = target_price, "hit_target"
                elif stop_hit_day:
                    exit_p, exit_r = stop_loss, "hit_stop"
                else:
                    # 持满窗口：用窗口末日收盘价（第 exit_window 个交易日）
                    last_idx = min(exit_window - 1, len(series) - 1)
                    exit_p   = float(series.iloc[last_idx])
                    exit_r   = f"held_to_t{exit_window}"

                if exit_p:
                    pred["effective_exit_pct"]    = round((exit_p - entry) / entry * 100, 4)
                    pred["effective_exit_reason"] = exit_r
                    # 退出质量：0=最差时机，1=最佳时机
                    rng = peak - trough
                    if rng > 0.01:
                        if final_dir == "看多":
                            pred["exit_quality"] = round((exit_p - trough) / rng, 3)
                        elif final_dir == "看空":
                            pred["exit_quality"] = round((peak - exit_p) / rng, 3)

                pred["exit_tracked"] = True
                updated = True

            # ── 出局后5日（检验是否出局过早，exit_tracked 完成后追加）──
            if pred.get("exit_tracked") and pred.get("post_exit_5d_pct") is None:
                post_idx = exit_window + 4   # 第 exit_window+5 个交易日（0-indexed）
                if len(series) > post_idx:
                    pred["post_exit_5d_pct"] = round(
                        (float(series.iloc[post_idx]) - entry) / entry * 100, 4
                    )
                    updated = True

    if updated:
        save_raw(data, path)
        logger.info("已更新多天复盘结果（T+1/T+3/T+5 + 退出追踪）")
    return updated


def _effective_correct(pred: dict) -> bool | None:
    """有效出局准确率：市场驱动退出 > T+3 > T+0，None 表示样本尚不可判定。"""
    if pred.get("exit_tracked") and pred.get("effective_exit_pct") is not None:
        ep = pred["effective_exit_pct"]
        d  = pred.get("final_direction", "中性")
        return (ep > 0) if d == "看多" else ((ep < 0) if d == "看空" else None)
    t3 = pred.get("t3_correct")
    return t3 if t3 is not None else pred.get("correct")


def _action_win_rate(action: str, path: str = None) -> tuple[float | None, int]:
    """统计历史上该 action（如'积极买入'）在看多方向的市场驱动出局胜率，供执行门禁使用。"""
    path = path or os.environ.get("PREDICTIONS_FILE", "/tmp/predictions.json")
    data = load_predictions(path)
    wins = total = 0
    for day in data.get("history", []):
        for p in day.get("predictions", []):
            if p.get("action") != action or p.get("final_direction") != "看多":
                continue
            c = _effective_correct(p)
            if c is None:
                continue
            total += 1
            if c:
                wins += 1
    if total == 0:
        return None, 0
    return wins / total, total


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


def _ai_review_analysis(
    reviewed: list[dict],
    scan_date: str,
    anthropic_key: str,
    macro_context: str = "",
) -> str:
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

    # 从 history 统计各股历史准确率
    path = os.environ.get("PREDICTIONS_FILE", "/tmp/predictions.json")
    hist_data = load_predictions(path)

    sym_stats = {}
    for day in hist_data.get("history", []):
        for p in day.get("predictions", []):
            s = p.get("symbol")
            c = _effective_correct(p)
            if s and c is not None:
                sym_stats.setdefault(s, [0, 0])
                sym_stats[s][1] += 1
                if c:
                    sym_stats[s][0] += 1
    acc_lines = [
        f"  {s}: {v[0]}/{v[1]}次正确（{v[0]/v[1]*100:.0f}%，市场驱动出局）"
        for s, v in sym_stats.items() if v[1] >= 5
    ]
    acc_block = ("历史准确率（≥5次，市场驱动出局为主）：\n" + "\n".join(acc_lines)) if acc_lines else ""

    macro_block = f"当日宏观：\n{macro_context}\n\n" if macro_context else ""

    prompt = (
        f"量化交易复盘分析，日期 {scan_date}：\n\n"
        f"{macro_block}"
        f"✅ 方向正确（{len(right)}只）：\n" + "\n".join(_fmt(r) for r in right) + "\n\n"
        f"❌ 方向错误（{len(wrong)}只）：\n" + "\n".join(_fmt(r) for r in wrong) + "\n\n"
        + (acc_block + "\n\n" if acc_block else "")
        + "用120字以内分析：①错误预测的共同特征或原因（结合宏观环境和历史准确率）；"
          "②今日扫描应重点注意什么。直接给出分析，不用列序号。"
    )
    try:
        client = anthropic.Anthropic(api_key=anthropic_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.warning("AI复盘分析失败: %s", e)
        return ""


async def _send_weekly_report(bot: Bot, chat_ids: list[int]) -> None:
    """发送上周交易周期的绩效周报（每周一扫描前触发）。"""
    path = os.environ.get("PREDICTIONS_FILE", "/tmp/predictions.json")
    data = load_predictions(path)
    if not data:
        return
    history = data.get("history", [])
    if not history:
        return
    # 取最近 7 天的 history 条目
    cutoff = datetime.date.today() - datetime.timedelta(days=7)
    recent = [
        d for d in history
        if d.get("scan_date", "") >= str(cutoff)
    ]
    if not recent:
        return
    msg = format_weekly_report(recent)
    if msg:
        for chat_id in chat_ids:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        logger.info("周报已发送")


async def _run_review(
    bot: Bot,
    chat_ids: list[int],
    anthropic_key: str = "",
    finnhub_client=None,
    strict_date_check: bool = True,
) -> None:
    """加载当日预测，对比实际收盘价，发送复盘消息。
    strict_date_check=True（独立 review 模式）：若 scan_date≠今日则跳过，防止节假日重复复盘。
    strict_date_check=False（all-in-one 模式）：允许复盘前一天的数据。
    """
    import yfinance as yf

    # 先填充 history 中待完成的 T+1/T+3/T+5
    try:
        _fill_multi_day_outcomes()
    except Exception as e:
        logger.warning("多天复盘填充失败: %s", e)

    # 收集近7天历史中已有 T+n 结果的条目，用于消息展示
    recent_multis: list[dict] = []
    try:
        _fresh = load_predictions()
        _today_d = datetime.date.today()
        for _day in _fresh.get("history", []):
            _d = _day.get("scan_date", "")
            if not _d:
                continue
            try:
                _age = (_today_d - datetime.date.fromisoformat(_d)).days
            except ValueError:
                continue
            if not (0 < _age <= 7):
                continue
            for _p in _day.get("predictions", []):
                if any(_p.get(k) is not None for k in ("t1_pct", "t3_pct", "t5_pct")):
                    recent_multis.append({**_p, "scan_date": _d})
    except Exception:
        pass

    data = load_predictions()
    if not data:
        logger.info("无预测记录，跳过复盘")
        return

    scan_date   = data.get("scan_date", "")
    predictions = data.get("predictions", [])
    if not predictions:
        return

    # 节假日检查：独立 review 模式下，scan_date≠今日说明今日扫描被跳过
    today_str = str(datetime.date.today())
    if strict_date_check and scan_date != today_str:
        logger.info("scan_date(%s) ≠ 今日(%s)，节假日跳过复盘", scan_date, today_str)
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

    # 宏观上下文（今日收盘后的 SPY/QQQ/VIX 状态）
    macro_context = ""
    try:
        macro_context = _get_macro_context(finnhub_client)
    except Exception:
        pass

    ai_analysis = ""
    if anthropic_key:
        ai_analysis = _ai_review_analysis(reviewed, scan_date, anthropic_key, macro_context)

    msg = format_review_message(scan_date, reviewed, ai_analysis=ai_analysis,
                               history_updates=recent_multis)
    if msg:
        msg = _safe_truncate(msg)
        for chat_id in chat_ids:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        logger.info("复盘消息已发送")


async def _run_scan(config, bot, chat_ids, finnhub_client, anthropic_key):
    """盘前扫描：分析信号、调用 AI、推送 Telegram、保存今日预测。"""
    if not _is_us_trading_day():
        logger.info("今日非美股交易日（节假日/周末），跳过盘前扫描")
        return

    # 周一额外发送上周绩效周报（先补全多天数据再统计）
    if datetime.date.today().weekday() == 0:
        try:
            _fill_multi_day_outcomes()
            await _send_weekly_report(bot, chat_ids)
        except Exception as e:
            logger.warning("周报发送失败: %s", e)

    symbols = watchlist_repo.get_all_symbols()
    pinned  = set(config.pinned_symbols)
    min_str = config.signals.get("min_strength", 30)
    use_ai  = bool(anthropic_key)

    # 市场初筛：从标普500+中概池里用动量/成交量打分选出候选，并入今日分析列表。
    # 不改动 watchlist_repo（不持久化），只影响当次扫描；失败不影响核心票扫描。
    discovered: set[str] = set()
    try:
        universe = list(dict.fromkeys(config.universe_sp500 + config.universe_china_adr))
        if universe:
            discovered = set(screen_top_candidates(
                universe, top_n=_SCREENER_TOP_N, exclude=set(symbols),
                finnhub_client=finnhub_client,
            ))
            symbols = symbols + [s for s in discovered if s not in symbols]
    except Exception as e:
        logger.warning("市场初筛失败，跳过: %s", e)

    logger.info("开始扫描 %d 只股票（核心%d + 初筛发现%d，AI分析: %s）→ 推送到 %d 个 chat",
                len(symbols), len(symbols) - len(discovered), len(discovered),
                "开启" if use_ai else "关闭", len(chat_ids))

    client = YFinanceDataClient()
    macro_context = _get_macro_context(finnhub_client)

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
            ai_failed = False
            if use_ai:
                try:
                    symbol_history = get_symbol_history(sym)
                    ai_result = run_ai_analysis(
                        result, finnhub_client, anthropic_key, macro_context,
                        symbol_history=symbol_history,
                    )
                    if ai_result:
                        logger.info("AI综合研判 %s: %s 置信度%s",
                                    sym, ai_result.get("final_direction", "?"),
                                    ai_result.get("conviction", "?"))
                    else:
                        ai_failed = True
                        logger.warning("AI 分析失败 %s：未生成结果，本条不作为执行建议", sym)
                except Exception as e:
                    ai_failed = True
                    logger.error("AI 分析失败 %s: %s", sym, e)

            price = result.current_price
            is_discovered = sym in discovered
            # AI 尝试研判但失败时，消息不能悄悄退回纯技术信号当"买入建议"——
            # 执行层（_run_execution）没有 AI action 就不会下单，消息和实际动作
            # 必须保持一致，否则用户会以为系统已经买了
            msg = format_signal_message(result, pinned=is_pinned, ai_result=ai_result or None,
                                        discovered=is_discovered, ai_failed=ai_failed)
            msg = _safe_truncate(msg)
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
                "horizon":         ai_result.get("horizon", "3-5天") if ai_result else "3-5天",
                "entry_price":     round(price, 4) if price else None,
                "target_price":    ai_result.get("target_price") if ai_result else None,
                "stop_loss":       ai_result.get("stop_loss") if ai_result else None,
                "verdict":         ai_result.get("verdict", "") if ai_result else "",
                "source":          "screener" if is_discovered else "core",
                "ai_failed":       ai_failed,
            })

            await asyncio.sleep(0.8)
        except Exception as e:
            logger.error("处理 %s 失败: %s", sym, e)
            try:
                err_msg = f"⚠️ <b>{sym}</b> 扫描失败：{_html.escape(str(e)[:200])}"
                for chat_id in chat_ids:
                    await bot.send_message(chat_id=chat_id, text=err_msg, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    logger.info("扫描完成，共推送 %d 条信号", pushed)

    if today_predictions:
        save_predictions(today_predictions)

    # ── 相关性风险提示 ─────────────────────────────────
    if len(today_predictions) >= 2:
        buy_syms = {p["symbol"] for p in today_predictions if p.get("final_direction") == "看多"}
        corr_warns = []
        for cluster_name, cluster_syms in _CORR_CLUSTERS.items():
            overlap = buy_syms & cluster_syms
            if len(overlap) >= 2:
                corr_warns.append((cluster_name, sorted(overlap)))
        if corr_warns:
            warn_lines = ["⚠️ <b>板块相关性提示</b>"]
            for cname, syms in corr_warns:
                warn_lines.append(f"  {cname}：{' / '.join(syms)} 同日看多")
            warn_lines.append("  → 上述标的高度正相关，同向加仓等同集中押注单一主题")
            warn_lines.append("  → 建议合并仓位不超 20%，或选择其中信号最强的一只")
            try:
                warn_msg = "\n".join(warn_lines)
                for chat_id in chat_ids:
                    await bot.send_message(chat_id=chat_id, text=warn_msg,
                                           parse_mode=ParseMode.HTML)
                logger.info("相关性提示已发送: %s", corr_warns)
            except Exception as e:
                logger.warning("相关性提示发送失败: %s", e)

    return today_predictions


async def _run_execution(
    bot: Bot,
    chat_ids: list[int],
    alpaca_key: str,
    alpaca_secret: str,
    today_predictions: list[dict],
    config,
) -> None:
    """
    盘前执行：把今日 AI 买入信号转化为 Alpaca 括号单。
    括号单包含入场限价 + 止损 + 止盈，由 Alpaca 服务端自动监控执行，
    不需要本地进程常驻，GitHub Actions 跑完即可关闭。
    """
    if not alpaca_key or not alpaca_secret:
        logger.info("ALPACA_API_KEY 未配置，跳过执行")
        return
    if not today_predictions:
        return

    from src.execution.alpaca_client import AlpacaClient
    alpaca = AlpacaClient(alpaca_key, alpaca_secret, paper=True)

    try:
        account   = alpaca.get_account()
        positions = alpaca.get_positions()
    except Exception as e:
        logger.error("获取 Alpaca 账户失败: %s", e)
        return

    held_symbols       = {p["symbol"] for p in positions}
    positions_by_symbol = {p["symbol"]: p for p in positions}
    # 仓位/敞口/止损计算只按自定的资金基数走（默认$50k），不用账户真实总权益（可能更高，如$100k纸面本金）
    capital_base   = min(account["equity"], config.alpaca_capital_base)
    deployed       = sum(p["market_value"] for p in positions)  # 按实际持仓市值算，不依赖broker总现金
    available_cash = min(capital_base - deployed, account["cash"])
    open_positions = len(held_symbols)

    # 构建 tier 映射（决定仓位上限）
    tier_map: dict[str, str] = {}
    for s in config.tier_core:        tier_map[s] = "core"
    for s in config.tier_swing:       tier_map[s] = "swing"
    for s in config.tier_speculative: tier_map[s] = "speculative"

    tier_cap = {"core": 0.15, "swing": 0.08, "speculative": 0.05}  # 仓位上限（占总资产）

    executed: list[dict] = []
    skipped:  list[str]  = []
    closed:   list[dict] = []
    win_rate_cache: dict[str, tuple] = {}  # action -> (win_rate, sample_n)，避免每个候选都重新读一遍predictions.json

    for pred in today_predictions:
        sym       = pred.get("symbol", "")
        action    = pred.get("action", "")
        final_dir = pred.get("final_direction", "中性")
        conviction = pred.get("conviction", "中")
        entry     = pred.get("entry_price")
        stop      = pred.get("stop_loss")
        target    = pred.get("target_price")

        # 看空或回避：若已有多头持仓则平仓
        if action in ("回避", "减仓") or final_dir == "看空":
            if sym in held_symbols:
                result = alpaca.close_position(sym)
                if result:
                    closed.append(result)
                    # 只有确认完全成交才释放持仓数/资金额度，避免用未成交的平仓
                    # 去解锁本轮后面的新买入（poll超时/部分成交时状态不是"filled"）
                    if result.get("status") == "filled":
                        open_positions -= 1
                        freed = positions_by_symbol.get(sym, {}).get("market_value", 0.0)
                        deployed       -= freed
                        available_cash += freed
            continue

        # 只处理明确买入信号
        if action not in ("积极买入", "谨慎买入") or final_dir != "看多":
            skipped.append(f"{sym}({action or '观望'})")
            continue

        # 已有持仓：跳过避免重复建仓
        if sym in held_symbols:
            skipped.append(f"{sym}(已持仓)")
            continue

        # 缺少止盈/止损价：跳过
        if not entry or not stop or not target:
            skipped.append(f"{sym}(缺价格参数)")
            continue

        # 硬顶1：最大持仓数
        if open_positions >= _MAX_POSITIONS:
            skipped.append(f"{sym}(已达最大持仓数{_MAX_POSITIONS})")
            continue

        # 硬顶2：总仓位敞口
        if deployed >= capital_base * _MAX_TOTAL_EXPOSURE_PCT:
            skipped.append(f"{sym}(总敞口已达上限)")
            continue

        # 门禁：该 action 历史胜率过低时暂停自动执行（样本不足时不生效）
        if action not in win_rate_cache:
            win_rate_cache[action] = _action_win_rate(action)
        win_rate, sample_n = win_rate_cache[action]
        if sample_n >= _MIN_WINRATE_SAMPLES and win_rate is not None and win_rate < _MIN_WINRATE:
            skipped.append(f"{sym}({action}历史胜率{win_rate*100:.0f}%过低)")
            continue

        # 仓位计算：按单笔最大亏损反推股数（入场价-止损价 为每股风险），
        # 再用 tier 上限、剩余现金、总敞口余量分别封顶取最小值
        risk_per_share = entry - stop
        if risk_per_share <= 0:
            skipped.append(f"{sym}(止损价格异常)")
            continue

        max_loss_usd = capital_base * _MAX_LOSS_PER_TRADE_PCT
        if action == "谨慎买入":
            max_loss_usd *= 0.5
        risk_based_amount = (max_loss_usd / risk_per_share) * entry

        # 市场初筛发现的新标的未经长期观察，仓位上限按更保守的speculative档处理
        default_tier    = "speculative" if pred.get("source") == "screener" else "swing"
        tier            = tier_map.get(sym, default_tier)
        tier_cap_amount = capital_base * tier_cap.get(tier, 0.05)
        exposure_room   = capital_base * _MAX_TOTAL_EXPOSURE_PCT - deployed

        position_amount = min(risk_based_amount, tier_cap_amount, exposure_room)
        # 单笔不超过剩余可用资金的 40%，防止满仓
        position_amount = min(position_amount, available_cash * 0.40)
        shares = int(position_amount / entry)

        if shares <= 0:
            skipped.append(f"{sym}(仓位不足1股)")
            continue
        if position_amount > available_cash:
            skipped.append(f"{sym}(现金不足)")
            continue

        result = alpaca.place_bracket_order(sym, shares, entry, stop, target)
        if result:
            executed.append(result)
            cost = shares * entry
            available_cash -= cost   # 预扣，防止后续超额
            deployed        += cost
            open_positions  += 1
        else:
            skipped.append(f"{sym}(下单失败)")

    # ── 发送执行报告 ──────────────────────────────────────
    lines = ["🤖 <b>Alpaca 模拟执行报告</b>", "─" * 28]
    lines.append(
        f"操作基数 <b>${capital_base:,.0f}</b>  "
        f"已用 ${deployed:,.0f}  可用 ${available_cash:,.0f}"
        f"（账户实际总权益 ${account['equity']:,.0f}，多余部分不参与计算）"
    )

    if executed:
        lines.append(f"\n✅ <b>已下单（{len(executed)}笔）</b>")
        for e in executed:
            upside   = (e["target"] - e["entry"]) / e["entry"] * 100
            downside = (e["entry"]  - e["stop"])  / e["entry"] * 100
            cost     = e["qty"] * e["entry"]
            lines.append(
                f"  {e['symbol']} ×{e['qty']}股 @ ${e['entry']:.2f}  "
                f"总花费 ${cost:,.2f}\n"
                f"    止损 ${e['stop']:.2f}(-{downside:.1f}%)  "
                f"止盈 ${e['target']:.2f}(+{upside:.1f}%)"
            )
    if closed:
        lines.append(f"\n🔴 <b>已平仓（{len(closed)}笔）</b>")
        for c in closed:
            price = c.get("filled_price")
            qty   = c.get("filled_qty") or 0
            if c.get("status") == "filled" and price:
                proceeds = qty * price
                lines.append(f"  {c['symbol']} ×{qty:.0f}股 @ ${price:.2f}  收入 ${proceeds:,.2f}")
            else:
                lines.append(f"  {c['symbol']}（成交价待确认，状态：{c.get('status', '?')}）")
    if skipped:
        lines.append(f"\n⏭ 跳过：{', '.join(skipped)}")

    if not executed and not closed:
        lines.append("\n今日无新操作")

    msg = "\n".join(lines)
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("执行报告发送失败: %s", e)
    logger.info("执行完成：下单 %d 笔，平仓 %d 笔，跳过 %d 笔",
                len(executed), len(closed), len(skipped))


async def _run_test_buy(
    bot: Bot,
    chat_ids: list[int],
    alpaca_key: str,
    alpaca_secret: str,
    symbol: str,
    qty: int,
) -> None:
    """
    手动验证单：简单市价买入，不走括号单/止盈止损，用于确认Alpaca对接是否成功。
    出场时机由使用者自行决定，仓位会自动出现在下次 _sync_portfolio 的持仓快报里。
    """
    if not alpaca_key or not alpaca_secret:
        logger.info("ALPACA_API_KEY 未配置，跳过测试单")
        return

    from src.execution.alpaca_client import AlpacaClient
    alpaca = AlpacaClient(alpaca_key, alpaca_secret, paper=True)

    result = alpaca.place_market_order(symbol, qty, side="buy")

    if result and result.get("status") == "filled":
        filled_qty = result["filled_qty"]
        price      = result["filled_price"]
        cost       = filled_qty * price
        try:
            cash_left = alpaca.get_account()["cash"]
            cash_line = f"剩余现金 ${cash_left:,.2f}"
        except Exception:
            cash_line = "剩余现金 获取失败"
        msg = (
            f"🧪 <b>Alpaca 测试单 - 买入</b>\n{'─' * 28}\n"
            f"✅ <b>{symbol}</b> ×{filled_qty:.0f}股 @ ${price:.2f}\n"
            f"总花费 ${cost:,.2f}\n"
            f"{cash_line}\n"
            f"订单号: {result['order_id']}\n\n"
            f"未设止盈止损，出场时机由你自行决定；仓位会体现在下次持仓快报里。"
        )
    elif result:
        msg = (
            f"🧪 <b>Alpaca 测试单 - 买入</b>\n{'─' * 28}\n"
            f"⚠️ {symbol} ×{qty} 已提交但未在轮询时间内确认成交（状态：{result['status']}），"
            f"订单号: {result['order_id']}，请稍后去 Alpaca Dashboard 核实"
        )
    else:
        msg = f"🧪 <b>Alpaca 测试单 - 买入</b>\n{'─' * 28}\n❌ {symbol} ×{qty} 下单失败，详见运行日志"

    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("测试单结果发送失败: %s", e)
    logger.info("测试买入执行完成: %s ×%d, 结果=%s", symbol, qty, "成功" if result else "失败")


async def _run_test_sell(
    bot: Bot,
    chat_ids: list[int],
    alpaca_key: str,
    alpaca_secret: str,
    symbol: str,
) -> None:
    """手动验证单：全部平仓指定symbol，用于确认卖出流程/回收测试仓位。"""
    if not alpaca_key or not alpaca_secret:
        logger.info("ALPACA_API_KEY 未配置，跳过测试卖单")
        return

    from src.execution.alpaca_client import AlpacaClient
    alpaca = AlpacaClient(alpaca_key, alpaca_secret, paper=True)

    result = alpaca.close_position(symbol)

    if result and result.get("status") == "filled":
        filled_qty = result["filled_qty"]
        price      = result["filled_price"]
        proceeds   = filled_qty * price
        try:
            cash_left = alpaca.get_account()["cash"]
            cash_line = f"剩余现金 ${cash_left:,.2f}"
        except Exception:
            cash_line = "剩余现金 获取失败"
        msg = (
            f"🧪 <b>Alpaca 测试单 - 卖出</b>\n{'─' * 28}\n"
            f"✅ <b>{symbol}</b> ×{filled_qty:.0f}股 @ ${price:.2f}\n"
            f"总收入 ${proceeds:,.2f}\n"
            f"{cash_line}\n"
            f"订单号: {result['order_id']}"
        )
    elif result:
        msg = (
            f"🧪 <b>Alpaca 测试单 - 卖出</b>\n{'─' * 28}\n"
            f"⚠️ {symbol} 已提交但未在轮询时间内确认成交（状态：{result['status']}），"
            f"订单号: {result['order_id']}，请稍后去 Alpaca Dashboard 核实"
        )
    else:
        msg = f"🧪 <b>Alpaca 测试单 - 卖出</b>\n{'─' * 28}\n❌ {symbol} 平仓失败，详见运行日志（可能本来就没有持仓）"

    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("测试卖单结果发送失败: %s", e)
    logger.info("测试卖出执行完成: %s, 结果=%s", symbol, "成功" if result else "失败")


async def _run_test_ai(bot: Bot, chat_ids: list[int], anthropic_key: str) -> None:
    """
    诊断用：单次最小 Anthropic API 调用，打开 httpx/httpcore 的 DEBUG 日志，
    把完整异常链路（含底层网络异常类型）报出来，用于排查生产环境反复出现的
    "Connection error"根因，不用跑一整轮36只股票的AI分析烧配额。
    """
    if not anthropic_key:
        logger.info("ANTHROPIC_API_KEY 未配置，跳过AI诊断")
        return

    import logging as _logging
    for name in ("httpx", "httpcore", "anthropic"):
        _logging.getLogger(name).setLevel(_logging.DEBUG)

    import traceback
    import anthropic

    client = anthropic.Anthropic(api_key=anthropic_key)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=20,
            messages=[{"role": "user", "content": "ping"}],
        )
        reply = msg.content[0].text[:100]
        result_text = f"✅ AI连接正常\n\n回复: {reply}"
        logger.info("AI诊断成功: %s", reply)
    except Exception as e:
        cause = getattr(e, "__cause__", None)
        result_text = (
            f"❌ AI连接失败\n\n"
            f"异常类型: {type(e).__name__}\n"
            f"消息: {e}\n"
            f"底层原因: {type(cause).__name__ if cause else '无'}: {cause}"
        )
        logger.error("AI诊断失败 类型=%s 消息=%s 底层类型=%s 底层消息=%s",
                     type(e).__name__, e, type(cause).__name__ if cause else None, cause)
        logger.error("完整traceback:\n%s", traceback.format_exc())

    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=result_text)
        except Exception as e:
            logger.warning("AI诊断结果发送失败: %s", e)


async def _sync_portfolio(
    bot: Bot,
    chat_ids: list[int],
    alpaca_key: str,
    alpaca_secret: str,
) -> None:
    """盘后同步：读取 Alpaca 实际持仓和盈亏，发送持仓快报到 Telegram。"""
    if not alpaca_key or not alpaca_secret:
        return

    from src.execution.alpaca_client import AlpacaClient
    alpaca = AlpacaClient(alpaca_key, alpaca_secret, paper=True)

    try:
        account   = alpaca.get_account()
        positions = alpaca.get_positions()
    except Exception as e:
        logger.error("同步 Alpaca 持仓失败: %s", e)
        return

    pl_icon  = "🟢" if account["today_pl"] >= 0 else "🔴"
    pl_sign  = "+" if account["today_pl"] >= 0 else ""
    deployed = account["equity"] - account["cash"]
    dep_pct  = deployed / account["equity"] * 100 if account["equity"] > 0 else 0

    lines = [
        "📊 <b>Alpaca 模拟持仓快报</b>",
        "─" * 28,
        f"总资产: <b>${account['equity']:,.0f}</b>  "
        f"{pl_icon} 今日 {pl_sign}${account['today_pl']:,.0f}"
        f"({pl_sign}{account['today_pl_pct']:.2f}%)",
        f"现金: ${account['cash']:,.0f}  "
        f"已用: ${deployed:,.0f} ({dep_pct:.1f}%)",
    ]

    if positions:
        lines.append(f"\n<b>持仓明细（{len(positions)}只）</b>")
        for p in sorted(positions, key=lambda x: x["unrealized_pl"], reverse=True):
            icon  = "🟢" if p["unrealized_pl"] >= 0 else "🔴"
            sign  = "+" if p["unrealized_pl"] >= 0 else ""
            price = f"${p['current_price']:.2f}" if p["current_price"] else "N/A"
            lines.append(
                f"  {icon} <b>{p['symbol']}</b> {p['qty']:.0f}股  "
                f"均价${p['avg_entry_price']:.2f}  现价{price}  "
                f"浮盈 {sign}${p['unrealized_pl']:,.0f}({sign}{p['unrealized_plpc']:.1f}%)"
            )
    else:
        lines.append("\n当前无持仓")

    msg = "\n".join(lines)
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("持仓快报发送失败: %s", e)
    logger.info("Alpaca 持仓快报已发送，持仓 %d 只", len(positions))


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
    alpaca_key     = os.environ.get("ALPACA_API_KEY", "").strip()
    alpaca_secret  = os.environ.get("ALPACA_API_SECRET", "").strip()
    finnhub_client = FinnhubClient(finnhub_key) if finnhub_key else None

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

    if scan_mode == "test_ai":
        try:
            await _run_test_ai(bot, chat_ids, anthropic_key)
        except Exception as e:
            logger.error("AI诊断失败: %s", e)
        return

    if scan_mode == "test_buy":
        test_symbol = os.environ.get("TEST_BUY_SYMBOL", "QQQ").upper()
        test_qty    = int(os.environ.get("TEST_BUY_QTY", "1"))
        try:
            await _run_test_buy(bot, chat_ids, alpaca_key, alpaca_secret, test_symbol, test_qty)
        except Exception as e:
            logger.error("测试单失败: %s", e)
        return

    if scan_mode == "test_sell":
        test_symbol = os.environ.get("TEST_SELL_SYMBOL", "QQQ").upper()
        try:
            await _run_test_sell(bot, chat_ids, alpaca_key, alpaca_secret, test_symbol)
        except Exception as e:
            logger.error("测试卖单失败: %s", e)
        return

    if scan_mode == "review":
        try:
            await _run_review(bot, chat_ids, anthropic_key, finnhub_client)
        except Exception as e:
            logger.error("复盘失败: %s", e)
        # 盘后同步 Alpaca 持仓快报
        try:
            await _sync_portfolio(bot, chat_ids, alpaca_key, alpaca_secret)
        except Exception as e:
            logger.warning("Alpaca 持仓同步失败: %s", e)

    elif scan_mode == "scan":
        today_preds = await _run_scan(config, bot, chat_ids, finnhub_client, anthropic_key)
        # 盘前执行：信号生成后立即下单
        try:
            await _run_execution(bot, chat_ids, alpaca_key, alpaca_secret,
                                 today_preds or [], config)
        except Exception as e:
            logger.error("Alpaca 执行失败: %s", e)

    else:
        # all-in-one 模式
        try:
            await _run_review(bot, chat_ids, anthropic_key, finnhub_client,
                              strict_date_check=False)
        except Exception as e:
            logger.error("复盘失败: %s", e)
        today_preds = await _run_scan(config, bot, chat_ids, finnhub_client, anthropic_key)
        try:
            await _run_execution(bot, chat_ids, alpaca_key, alpaca_secret,
                                 today_preds or [], config)
        except Exception as e:
            logger.error("Alpaca 执行失败: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
