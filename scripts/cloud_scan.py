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

# ── 盘前/盘后哨兵参数 ──────────────────────────────────────
_EXTENDED_MOVE_THRESHOLD_PCT  = 3.0   # 相对昨收涨跌幅超过这个才触发完整技术+AI分析
_EXTENDED_RETRY_THRESHOLD_PCT = 2.0   # 挂单未成交、价格继续朝买入方向运行超过这个才撤单重挂
_EXTENDED_MAX_RETRIES         = 1     # 每个标的每个时段最多重挂几次，防止单边行情反复触发AI分析

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
from src.analysis.shadow_analyst import run_shadow_analysis
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


_MIN_CALIBRATION_SAMPLES = 6   # 校准维度样本不足时不展示，避免小样本噪音被当成规律


def _compute_ai_calibration(path: str = None) -> str:
    """
    AI自我校准摘要：不是训练模型，是把系统级的历史表现（不是单只股票的，是
    across所有股票聚合的）显式喂回下一次分析的prompt里，让AI"看见"自己过去
    在不同维度上的判断准不准，而不是每次都从零开始判断。
    这是当前架构下能做到的、诚实的"自我改进"方式——没有模型权重更新，靠的是
    每次调用都带着最新的战绩简报。

    统计四个维度（各自样本量不足时跳过，不展示）：
    - 置信度校准：说"高"置信度时，历史上到底有多准（跨全部股票，样本量比
      单只股票大得多，统计意义更强）
    - 推翻vs确认：AI推翻技术信号时，比起单纯确认，历史上谁更准——如果推翻
      的胜率明显更低，说明该抬高推翻的门槛
    - 来源校准：初筛发现的新标的 vs 固定核心票，判断准确率是否有系统性差异
    - 触发时段校准：盘前/盘后哨兵触发的判断 vs 常规时段的判断，准确率是否
      有系统性差异（比如盘前盘后信息更少、更容易受情绪性过冲干扰）
    """
    path = path or os.environ.get("PREDICTIONS_FILE", "/tmp/predictions.json")
    data = load_predictions(path)
    all_preds = [p for day in data.get("history", []) for p in day.get("predictions", [])]
    if len(all_preds) < _MIN_CALIBRATION_SAMPLES:
        return ""

    def _bucket_rate(preds: list[dict], key_fn) -> dict:
        buckets: dict = {}
        for p in preds:
            c = _effective_correct(p)
            if c is None:
                continue
            key = key_fn(p)
            if key is None:
                continue
            buckets.setdefault(key, []).append(c)
        return {k: (sum(v) / len(v), len(v)) for k, v in buckets.items()
                if len(v) >= _MIN_CALIBRATION_SAMPLES}

    lines = []

    conv_rates = _bucket_rate(all_preds, lambda p: p.get("conviction") or None)
    if conv_rates:
        parts = [f"{k}{v[0]*100:.0f}%({v[1]}次)" for k, v in
                  sorted(conv_rates.items(), key=lambda x: -x[1][0])]
        lines.append(f"置信度校准（全部标的聚合，非本股票）：{' / '.join(parts)}")

    confirm_rates = _bucket_rate(
        all_preds, lambda p: {"True": "确认技术信号", "False": "推翻技术信号"}.get(str(p.get("tech_confirmed")))
    )
    if confirm_rates:
        parts = [f"{k} {v[0]*100:.0f}%({v[1]}次)" for k, v in confirm_rates.items()]
        lines.append(f"确认vs推翻校准：{' / '.join(parts)}")
        conf = confirm_rates.get("确认技术信号")
        override = confirm_rates.get("推翻技术信号")
        if conf and override and override[0] < conf[0] - 0.1:
            lines.append("  → 推翻技术信号的历史胜率明显低于确认，除非证据非常充分，倾向于相信技术面")

    source_rates = _bucket_rate(
        all_preds, lambda p: {"core": "固定核心票", "screener": "初筛新发现"}.get(p.get("source"))
    )
    if source_rates:
        parts = [f"{k} {v[0]*100:.0f}%({v[1]}次)" for k, v in source_rates.items()]
        lines.append(f"来源校准：{' / '.join(parts)}")

    session_rates = _bucket_rate(
        all_preds, lambda p: {
            "extended_pre": "盘前触发", "extended_post": "盘后触发",
        }.get(p.get("trigger_session"), "常规时段触发")
    )
    if session_rates:
        parts = [f"{k} {v[0]*100:.0f}%({v[1]}次)" for k, v in session_rates.items()]
        lines.append(f"触发时段校准：{' / '.join(parts)}")

    if not lines:
        return ""
    return "系统级历史校准（供参考，反映过去判断的系统性偏差，不是硬性规则）：\n" + "\n".join(lines)


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

    # AI自我校准：把系统级历史表现带进每一次分析（见 _compute_ai_calibration
    # 说明），这个函数本身每次运行只调用一次（不是每只股票都算一遍）
    try:
        calibration = _compute_ai_calibration()
        if calibration:
            parts.append(calibration)
    except Exception as e:
        logger.debug("AI校准摘要计算失败: %s", e)

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


async def _run_scan(config, bot, chat_ids, finnhub_client, anthropic_key, gemini_key=""):
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
    use_shadow = bool(gemini_key)

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

    logger.info("开始扫描 %d 只股票（核心%d + 初筛发现%d，AI分析: %s，影子模式: %s）→ 推送到 %d 个 chat",
                len(symbols), len(symbols) - len(discovered), len(discovered),
                "开启" if use_ai else "关闭", "开启" if use_shadow else "关闭", len(chat_ids))

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

            # 影子模式：免费模型独立跑一遍同样的输入，只记录不下单，
            # 不看Claude是否成功——两边判断需要各自独立，不能互相依赖
            shadow_result = {}
            if use_shadow:
                try:
                    shadow_result = run_shadow_analysis(
                        result, finnhub_client, gemini_key, macro_context,
                        symbol_history=get_symbol_history(sym),
                    )
                except Exception as e:
                    logger.info("影子模式 %s 异常，跳过（不影响主流程）: %s", sym, e)

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
                "tech_confirmed":  ai_result.get("tech_confirmed") if ai_result else None,
                # 影子模式：免费模型(Gemini)独立跑的判断，只留对比用的关键字段，
                # 不进任何执行/校准逻辑——避免JSON体积膨胀，也避免被误用为下单依据
                "shadow_verdict": ({
                    "model":          shadow_result.get("_shadow_model"),
                    "final_direction": shadow_result.get("final_direction"),
                    "conviction":      shadow_result.get("conviction"),
                    "action":          shadow_result.get("action"),
                    "tech_confirmed":  shadow_result.get("tech_confirmed"),
                    "verdict":         shadow_result.get("verdict"),
                } if shadow_result else None),
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


def _build_tier_maps(config) -> tuple[dict, dict]:
    """构建 tier 映射（决定仓位上限）。_run_execution 和 _run_extended_watch 共用。"""
    tier_map: dict[str, str] = {}
    for s in config.tier_core:        tier_map[s] = "core"
    for s in config.tier_swing:       tier_map[s] = "swing"
    for s in config.tier_speculative: tier_map[s] = "speculative"
    tier_cap = {"core": 0.15, "swing": 0.08, "speculative": 0.05}  # 仓位上限（占总资产）
    return tier_map, tier_cap


def _gate_and_size_position(
    sym: str, action: str, final_dir: str,
    entry: float, stop: float, target: float,
    source: str,
    held_symbols: set, open_positions: int, deployed: float, available_cash: float,
    capital_base: float, tier_map: dict, tier_cap: dict, win_rate_cache: dict,
) -> tuple[int | None, str | None]:
    """
    买入前的全部风控门禁 + 仓位计算。_run_execution（常规时段）和
    _run_extended_watch（盘前盘后哨兵）共用同一套逻辑，扩展时段不允许
    绕开任何一道门禁（最大持仓数/总敞口/历史胜率/单笔最大亏损）。
    返回 (股数, None) 表示可以下单；(None, 跳过原因) 表示不下单。
    """
    if action not in ("积极买入", "谨慎买入") or final_dir != "看多":
        return None, f"{sym}({action or '观望'})"

    if sym in held_symbols:
        return None, f"{sym}(已持仓)"

    if not entry or not stop or not target:
        return None, f"{sym}(缺价格参数)"

    if open_positions >= _MAX_POSITIONS:
        return None, f"{sym}(已达最大持仓数{_MAX_POSITIONS})"

    if deployed >= capital_base * _MAX_TOTAL_EXPOSURE_PCT:
        return None, f"{sym}(总敞口已达上限)"

    if action not in win_rate_cache:
        win_rate_cache[action] = _action_win_rate(action)
    win_rate, sample_n = win_rate_cache[action]
    if sample_n >= _MIN_WINRATE_SAMPLES and win_rate is not None and win_rate < _MIN_WINRATE:
        return None, f"{sym}({action}历史胜率{win_rate*100:.0f}%过低)"

    # 仓位计算：按单笔最大亏损反推股数（入场价-止损价 为每股风险），
    # 再用 tier 上限、剩余现金、总敞口余量分别封顶取最小值
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return None, f"{sym}(止损价格异常)"

    max_loss_usd = capital_base * _MAX_LOSS_PER_TRADE_PCT
    if action == "谨慎买入":
        max_loss_usd *= 0.5
    risk_based_amount = (max_loss_usd / risk_per_share) * entry

    # 市场初筛发现的新标的未经长期观察，仓位上限按更保守的speculative档处理
    default_tier    = "speculative" if source == "screener" else "swing"
    tier            = tier_map.get(sym, default_tier)
    tier_cap_amount = capital_base * tier_cap.get(tier, 0.05)
    exposure_room   = capital_base * _MAX_TOTAL_EXPOSURE_PCT - deployed

    position_amount = min(risk_based_amount, tier_cap_amount, exposure_room)
    # 单笔不超过剩余可用资金的 40%，防止满仓
    position_amount = min(position_amount, available_cash * 0.40)
    shares = int(position_amount / entry)

    if shares <= 0:
        return None, f"{sym}(仓位不足1股)"
    if position_amount > available_cash:
        return None, f"{sym}(现金不足)"

    return shares, None


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
    tier_map, tier_cap = _build_tier_maps(config)

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

        shares, skip_reason = _gate_and_size_position(
            sym, action, final_dir, entry, stop, target,
            pred.get("source", "core"),
            held_symbols, open_positions, deployed, available_cash,
            capital_base, tier_map, tier_cap, win_rate_cache,
        )
        if skip_reason:
            skipped.append(skip_reason)
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


def _current_extended_session() -> str | None:
    """判断当前处于盘前('pre')/盘后('post')/都不是(None)。按美东时区判断。"""
    from zoneinfo import ZoneInfo
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return None
    t = now.time()
    if datetime.time(4, 0) <= t < datetime.time(9, 30):
        return "pre"
    if datetime.time(16, 0) <= t < datetime.time(20, 0):
        return "post"
    return None


async def _run_extended_watch(
    config, bot, chat_ids, finnhub_client, anthropic_key, alpaca_key, alpaca_secret,
) -> None:
    """
    盘前/盘后哨兵：每次触发只用免费的 yfinance 分钟线扫一遍"核心watchlist +
    最近一次常规扫描初筛出的标的"的最新价（不烧AI配额），只有涨跌幅超过
    _EXTENDED_MOVE_THRESHOLD_PCT 才触发完整技术+AI分析——把AI调用限制在
    "真正有异动"的标的上，不是每次轮询都花钱，也不是每20分钟重新跑一次
    全市场初筛（复用9:07 ET那次常规扫描已经算好的结果）。

    确认买入信号后用纯限价单(extended_hours=True)入场——Alpaca扩展时段只
    接受纯限价单，没法带止损止盈的括号单保护——所以下单后记入
    pending_protection，等常规时段开盘由 _attach_pending_protection() 补挂。

    如果AI判断持有中的标的转为看空/回避，盘前盘后也会平仓——跟常规时段
    (_run_execution)判断逻辑一致，只是执行手段不同：close_position()内部
    是市价单，扩展时段一律拒收，这里用纯限价卖单(close_position_extended_hours)，
    如果原持仓挂了止损止盈保护会先撤掉再挂平仓单。

    同一个标的同一个时段（当天pre或post）只触发一次完整分析，避免异动持续
    几小时时每20分钟都重新分析一遍、重复推送。

    只有真正下单/平仓时才发Telegram通知（带上"本时段共触发N次分析"），单纯
    触发分析但没动作不通知，避免异动一多就刷屏。
    """
    if not _is_us_trading_day():
        logger.info("今日非美股交易日（节假日/周末/凌晨当日数据尚未生成），跳过哨兵检测")
        return
    session = _current_extended_session()
    if not session:
        logger.info("当前不在盘前/盘后时段，跳过哨兵检测")
        return

    today_str  = str(datetime.date.today())
    alert_key  = f"{today_str}_{session}"

    data              = load_predictions()
    extended_alerts   = dict(data.get("extended_alerts", {}))
    already_alerted   = set(extended_alerts.get(alert_key, []))
    pending_entries   = list(data.get("pending_entries", []))
    _entries_at_load  = list(pending_entries)  # 快照，写回时用来判断哪些是本轮"处理过"的
    trigger_counts    = dict(data.get("extended_trigger_count", {}))
    trigger_count     = trigger_counts.get(alert_key, 0)

    # pending_protection 不在本函数里维护完整列表——最终写回时按 order_id
    # 精确增删（见函数末尾），避免"先在本地列表里增删元素、再用长度切片算新增
    # 项"这种写法在reconcile阶段先删后loop阶段再加时算错新增范围
    removed_protection_ids: set[str] = set()
    added_protection: list[dict] = []

    # 检测范围：固定核心票 + 当天/最近一次常规扫描里市场初筛选出的标的
    # （不是只盯用户的固定清单，也不是每20分钟重新跑一次全市场初筛——
    # 复用 predictions.json 里最近一次常规扫描（9:07 ET）已经算好的结果，
    # 免费又不重复计算）
    core_symbols     = watchlist_repo.get_all_symbols()
    screener_symbols = sorted({
        p["symbol"] for p in data.get("predictions", [])
        if p.get("source") == "screener"
    })
    symbols = list(dict.fromkeys(core_symbols + screener_symbols))
    client  = YFinanceDataClient()

    use_ai = bool(anthropic_key)
    alpaca = None
    account = positions = None
    held_symbols: set = set()
    capital_base = deployed = available_cash = 0.0
    open_positions = 0
    tier_map = tier_cap = {}
    win_rate_cache: dict[str, tuple] = {}

    positions_by_symbol: dict = {}
    if alpaca_key and alpaca_secret:
        from src.execution.alpaca_client import AlpacaClient
        alpaca = AlpacaClient(alpaca_key, alpaca_secret, paper=True)
        try:
            account   = alpaca.get_account()
            positions = alpaca.get_positions()
            held_symbols   = {p["symbol"] for p in positions}
            positions_by_symbol = {p["symbol"]: p for p in positions}
            capital_base   = min(account["equity"], config.alpaca_capital_base)
            deployed       = sum(p["market_value"] for p in positions)
            available_cash = min(capital_base - deployed, account["cash"])
            open_positions = len(held_symbols)
            tier_map, tier_cap = _build_tier_maps(config)
        except Exception as e:
            logger.error("哨兵获取 Alpaca 账户失败，本轮不下单: %s", e)
            alpaca = None

    # ── 重挂检查：上一轮挂的盘前盘后限价单成交了吗？没成交且价格已经继续
    # 朝买入方向运行超过阈值——说明原来那个价位已经买不到了，撤单按新价重挂，
    # 让AI重新评估这个更高的价位还值不值得追（不是机械地"价格涨多少就跟着追多少"）
    still_pending_entries: list[dict] = []
    retry_candidates: list[tuple] = []   # (symbol, quote, retry_count)
    for entry in pending_entries:
        if entry.get("alert_key") != alert_key:
            continue  # 不是本时段挂的单，DAY单早随Alpaca自动过期，直接丢弃
        sym = entry["symbol"]
        if sym in held_symbols:
            continue  # 已成交，交给pending_protection那条线去补保护，这里不用管了
        q_now = client.get_extended_hours_quote(sym)
        if not q_now:
            still_pending_entries.append(entry)
            continue
        moved_pct = (q_now["price"] - entry["entry_price"]) / entry["entry_price"] * 100
        retry_count = entry.get("retry_count", 0)
        if moved_pct >= _EXTENDED_RETRY_THRESHOLD_PCT and retry_count < _EXTENDED_MAX_RETRIES and alpaca:
            # 撤单失败最可能的原因就是"刚好在这一刻成交了"（撤单和成交的竞态）——
            # 这种情况绝不能按"重挂"处理：那样会把这笔已经成交、且刚被判定为
            # "未持有"（上面held_symbols检查用的是本轮开始时的快照）的仓位的
            # 保护记录删掉，还会再开一张重复的买单，变成裸仓+超额下单两个问题
            # 叠一起。撤单失败就原样保留，让下一轮用最新的持仓快照重新判断
            if alpaca.cancel_order(entry["order_id"]):
                removed_protection_ids.add(entry["order_id"])
                already_alerted.discard(sym)
                retry_candidates.append((sym, q_now, retry_count + 1))
                logger.info("哨兵 %s 原挂单$%.2f未成交，现价$%.2f（+%.1f%%），撤单重挂（第%d次重试）",
                            sym, entry["entry_price"], q_now["price"], moved_pct, retry_count + 1)
            else:
                logger.warning("哨兵 %s 撤单失败（可能刚好成交），本轮不重挂，留给下一轮重新判断", sym)
                still_pending_entries.append(entry)
        else:
            still_pending_entries.append(entry)

    retry_syms = {c[0] for c in retry_candidates}

    candidates = list(retry_candidates)
    for sym in symbols:
        if sym in already_alerted or sym in retry_syms:
            continue
        q = client.get_extended_hours_quote(sym)
        if not q:
            continue
        chg = q["change_pct"]
        logger.info("哨兵 %s: %s时段 %+.2f%%（现价$%.2f / 前收$%.2f）",
                    sym, session, chg, q["price"], q["prev_close"])
        if abs(chg) >= _EXTENDED_MOVE_THRESHOLD_PCT:
            candidates.append((sym, q, 0))

    session_label = "盘前" if session == "pre" else "盘后"
    trigger_count_start = trigger_count
    newly_placed_entries: list[dict] = []
    newly_recorded_predictions: list[dict] = []   # 真正下单的才记进predictions.json，供复盘/校准使用

    if candidates:
        logger.info("哨兵发现 %d 只候选（含%d个重挂）: %s",
                    len(candidates), len(retry_candidates), [c[0] for c in candidates])
        macro_context = _get_macro_context(finnhub_client)

    for sym, q, retry_count in candidates:
        already_alerted.add(sym)
        trigger_count += 1
        try:
            result = analyze_symbol(
                client, sym,
                config.signals.get("lookback_days", 250),
                config.signals.get("lookback_weeks", 104),
                config.total_capital, config.max_position_pct,
            )
        except Exception as e:
            logger.error("哨兵技术分析 %s 失败: %s", sym, e)
            continue

        ai_result = {}
        if use_ai:
            try:
                symbol_history = get_symbol_history(sym)
                extended_note = (
                    f"【{session_label}异动提醒】{sym} 相对昨日收盘 {q['change_pct']:+.2f}%"
                    f"（现价${q['price']:.2f}，昨收${q['prev_close']:.2f}），"
                    f"这个异动发生在{session_label}时段，尚未反映在常规日线技术指标里，"
                    f"请结合基本面/消息面判断是追涨机会还是情绪性过冲，不要单纯因为已经涨了就回避，"
                    f"也不要单纯因为技术面滞后就无视这个价格变化。"
                )
                ai_result = run_ai_analysis(
                    result, finnhub_client, anthropic_key,
                    macro_context=(macro_context + "\n\n" + extended_note),
                    symbol_history=symbol_history,
                )
                if ai_result:
                    logger.info("哨兵AI研判 %s: %s 置信度%s（第%d次触发）",
                                sym, ai_result.get("final_direction", "?"),
                                ai_result.get("conviction", "?"), trigger_count)
            except Exception as e:
                logger.error("哨兵AI分析 %s 失败: %s", sym, e)

        action    = ai_result.get("action", "") if ai_result else ""
        final_dir = ai_result.get("final_direction", "中性") if ai_result else "中性"

        if not (alpaca and ai_result):
            logger.info("哨兵 %s 本次不下单（无AI结果或Alpaca未就绪）", sym)
            continue

        # 看空/回避且当前持有：盘前盘后也平仓，跟常规时段(_run_execution)判断
        # 逻辑一致，只是执行手段不同——close_position()内部是市价单，扩展时段
        # 一律拒收，只能用纯限价卖单(close_position_extended_hours)
        if (action in ("回避", "减仓") or final_dir == "看空") and sym in held_symbols:
            pos = positions_by_symbol.get(sym, {})
            qty = int(pos.get("qty", 0))
            if qty > 0:
                result = alpaca.close_position_extended_hours(sym, qty, q["price"])
                if result:
                    held_symbols.discard(sym)
                    open_positions -= 1
                    freed = pos.get("market_value", 0.0)
                    deployed       -= freed
                    available_cash += freed
                    msg = (
                        f"🌙 <b>{session_label}异动平仓</b>\n{'─'*28}\n"
                        f"<b>{sym}</b>  相对昨收 {q['change_pct']:+.2f}%  现价 ${q['price']:.2f}\n"
                        f"AI研判: {final_dir} / {action}（置信度{ai_result.get('conviction','?')}）\n"
                        f"\n✅ 已挂{session_label}平仓限价单 ×{qty}股 @${q['price']:.2f}"
                        f"（限价单，未必立即成交；若已有止损止盈保护单会先撤掉）"
                    )
                    for chat_id in chat_ids:
                        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
            continue

        entry = q["price"]
        stop  = ai_result.get("stop_loss")
        target = ai_result.get("target_price")
        # 初筛发现的标的（不在用户固定tier里）按更保守的speculative档处理，
        # 跟常规扫描（_run_scan/_run_execution）的口径保持一致
        source = "screener" if sym in screener_symbols else "core"
        shares, skip_reason = _gate_and_size_position(
            sym, action, final_dir, entry, stop, target, source,
            held_symbols, open_positions, deployed, available_cash,
            capital_base, tier_map, tier_cap, win_rate_cache,
        )
        if not shares:
            logger.info("哨兵 %s 不下单：%s", sym, skip_reason)
            continue

        order = alpaca.place_extended_hours_order(sym, shares, entry)
        if not order:
            continue

        held_symbols.add(sym)
        open_positions += 1
        cost = shares * entry
        deployed += cost
        available_cash -= cost
        added_protection.append({
            "symbol":    sym,
            "qty":       shares,
            "stop":      stop,
            "target":    target,
            "order_id":  order["order_id"],
            "opened_at": datetime.datetime.now().isoformat(),
        })
        newly_placed_entries.append({
            "symbol":      sym,
            "order_id":    order["order_id"],
            "entry_price": entry,
            "retry_count": retry_count,
            "alert_key":   alert_key,
        })
        # 记进predictions.json主列表，跟常规扫描的信号用同一套复盘/校准逻辑，
        # 否则盘前盘后触发的判断永远不会被复盘，也就永远进不了_compute_ai_calibration
        newly_recorded_predictions.append({
            "symbol":          sym,
            "direction":       "BUY",
            "final_direction": final_dir,
            "action":          action,
            "conviction":      ai_result.get("conviction", ""),
            "horizon":         ai_result.get("horizon", "3-5天"),
            "entry_price":     round(entry, 4),
            "target_price":    target,
            "stop_loss":       stop,
            "verdict":         ai_result.get("verdict", ""),
            "source":          source,   # core/screener，跟常规扫描口径一致，不要跟触发时段混在一个字段里
            "trigger_session": f"extended_{session}",  # 额外记录触发时段，供校准单独统计
            "ai_failed":       False,
            "tech_confirmed":  ai_result.get("tech_confirmed"),
        })

        retry_note = f"（第{retry_count + 1}次尝试后成交挂单）" if retry_count else ""
        msg = (
            f"🌙 <b>{session_label}异动买入</b>{retry_note}\n{'─'*28}\n"
            f"<b>{sym}</b>  相对昨收 {q['change_pct']:+.2f}%  现价 ${q['price']:.2f}\n"
            f"AI研判: {final_dir} / {action}（置信度{ai_result.get('conviction','?')}）\n"
            f"{('本' + session_label + '共触发' + str(trigger_count - trigger_count_start) + '次分析，这是其中筛出的买入')}\n"
        )
        if ai_result.get("verdict"):
            msg += f"{_html.escape(str(ai_result['verdict'])[:300])}\n"
        msg += (f"\n✅ 已挂{session_label}限价单 ×{shares}股 @${entry:.2f}"
                f"（无保护，等常规时段开盘补挂 止损${stop:.2f}/止盈${target:.2f}）")

        for chat_id in chat_ids:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

    # 真正下单的记进predictions.json主列表，复用save_predictions()已有的
    # "同一天二次扫描按symbol合并"逻辑——必须在下面读fresh之前调用，
    # 这样fresh读到的就是包含这次新记录的最新版本，不会被后面的save_raw覆盖掉
    if newly_recorded_predictions:
        save_predictions(newly_recorded_predictions)

    # 重新读一遍最新数据再合并写回，避免和并发跑的常规扫描/复盘/别的哨兵tick
    # 互相覆盖（这几个cron间隔只有20分钟，触发延迟时完全可能重叠执行）。
    # pending_entries/pending_protection 都用 order_id 精确匹配增删，而不是
    # 整体覆盖，避免丢掉并发tick期间新增的条目
    trigger_increment = trigger_count - trigger_count_start
    processed_entry_ids = {e["order_id"] for e in _entries_at_load if e.get("alert_key") == alert_key}

    fresh = load_predictions()
    fresh_alerts = dict(fresh.get("extended_alerts", {}))
    fresh_alerts[alert_key] = sorted(set(fresh_alerts.get(alert_key, [])) | already_alerted)
    fresh_counts = dict(fresh.get("extended_trigger_count", {}))
    fresh_counts[alert_key] = fresh_counts.get(alert_key, 0) + trigger_increment
    # 顺带清掉跨天的陈旧条目：DAY单不会跨自然日存活，昨天及更早、没被今天
    # 任何一次哨兵tick处理过的条目一定已经过期，否则会在pending_entries里
    # 永远堆积（旧alert_key不会再被今天的reconcile循环命中）
    fresh_entries = [
        e for e in fresh.get("pending_entries", [])
        if e["order_id"] not in processed_entry_ids and e.get("alert_key", "").startswith(today_str)
    ] + still_pending_entries + newly_placed_entries

    fresh_protection = [
        p for p in fresh.get("pending_protection", [])
        if p.get("order_id") not in removed_protection_ids
    ] + added_protection

    fresh["extended_alerts"]         = fresh_alerts
    fresh["extended_trigger_count"]  = fresh_counts
    fresh["pending_protection"]      = fresh_protection
    fresh["pending_entries"]         = fresh_entries
    save_raw(fresh)


async def _attach_pending_protection(
    bot, chat_ids, alpaca_key: str, alpaca_secret: str,
) -> None:
    """
    补挂保护：检查 pending_protection 里盘前/盘后裸单入场的仓位，
    只要 Alpaca 那边已经显示实际持仓（说明限价单成交了），就补上止损止盈
    OCO 保护单。在常规时段一开盘（_run_scan 最前面）调用。
    未成交、当天也没成交的裸单会在 Alpaca 那边随 DAY 单自动过期作废，
    这里直接从待办列表里清掉即可，不用额外撤单。
    """
    if not alpaca_key or not alpaca_secret:
        return
    data = load_predictions()
    pending = list(data.get("pending_protection", []))
    if not pending:
        return

    from src.execution.alpaca_client import AlpacaClient
    alpaca = AlpacaClient(alpaca_key, alpaca_secret, paper=True)
    try:
        positions = alpaca.get_positions()
    except Exception as e:
        logger.error("补挂保护：获取持仓失败: %s", e)
        return
    held_qty = {p["symbol"]: p["qty"] for p in positions}

    still_pending = []
    attached = []
    for item in pending:
        sym = item["symbol"]
        actual_qty = held_qty.get(sym, 0)
        if actual_qty <= 0:
            logger.info("补挂保护：%s 盘前盘后限价单未成交（已随DAY单过期），移出待办", sym)
            continue
        qty = min(item["qty"], int(actual_qty))
        result = alpaca.attach_protection(sym, qty, item["stop"], item["target"])
        if result:
            attached.append({**item, "qty": qty})
        else:
            still_pending.append(item)  # 补挂失败，留到下次再试

    # 重新读一遍最新数据：只摘除"本轮明确处理过"的条目(按order_id匹配)，
    # 保留期间可能被并发哨兵tick新加进来的条目，避免互相覆盖
    processed_ids = {item["order_id"] for item in pending}
    fresh = load_predictions()
    fresh["pending_protection"] = [
        item for item in fresh.get("pending_protection", [])
        if item["order_id"] not in processed_ids
    ] + still_pending
    save_raw(fresh)

    if attached:
        lines = ["🛡 <b>盘前/盘后仓位补挂保护</b>", "─" * 28]
        for a in attached:
            lines.append(f"  {a['symbol']} ×{a['qty']}股  止损${a['stop']:.2f}  止盈${a['target']:.2f}")
        msg = "\n".join(lines)
        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning("补挂保护通知发送失败: %s", e)
        logger.info("补挂保护完成：%d 笔", len(attached))


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


async def _run_test_shadow(bot: Bot, chat_ids: list[int], gemini_key: str) -> None:
    """
    诊断用：单次最小 Gemini API 调用，验证 GEMINI_API_KEY 这个 GitHub Secret
    在云端 runner 上确实能连通、密钥没有被截断或带上尾随换行——不用等真正
    盘前扫描触发影子模式才发现配置错了。
    """
    if not gemini_key:
        text = "❌ GEMINI_API_KEY 未配置，跳过影子模式诊断"
        logger.info(text)
    else:
        import requests
        from src.analysis.shadow_analyst import _GEMINI_MODEL, _GEMINI_URL
        try:
            resp = requests.post(
                _GEMINI_URL,
                params={"key": gemini_key},
                json={"contents": [{"role": "user", "parts": [{"text": "reply with the single word OK"}]}]},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            reply = data["candidates"][0]["content"]["parts"][0]["text"][:100]
            text = f"✅ 影子模式(Gemini)连接正常\n模型: {_GEMINI_MODEL}\n回复: {reply}"
            logger.info("影子模式诊断成功: %s", reply)
        except Exception as e:
            text = f"❌ 影子模式(Gemini)连接失败\n异常类型: {type(e).__name__}\n消息: {e}"
            logger.error("影子模式诊断失败 类型=%s 消息=%s", type(e).__name__, e)

    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning("影子模式诊断结果发送失败: %s", e)


async def _run_test_data(bot: Bot, chat_ids: list[int], alpaca_key: str, alpaca_secret: str) -> None:
    """
    诊断用：查询 Alpaca 免费(IEX)行情在盘前/盘后时段是否有真实报价更新，
    还是像 Finnhub 免费版一样冻结在收盘价。查最新成交(trade)和报价(quote)
    的时间戳，判断是否覆盖 4:00-9:30 / 16:00-20:00 ET 扩展时段。
    """
    if not alpaca_key or not alpaca_secret:
        logger.info("ALPACA_API_KEY 未配置，跳过行情诊断")
        return

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest, StockLatestQuoteRequest

    symbols = ["AAPL", "QQQ", "MRVL"]
    client = StockHistoricalDataClient(alpaca_key, alpaca_secret)

    lines = ["📡 <b>Alpaca 行情诊断</b>", "─" * 28]
    try:
        trades = client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbols))
        quotes = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbols))
        import datetime
        now_et = datetime.datetime.now(datetime.timezone.utc)
        for sym in symbols:
            t = trades.get(sym)
            q = quotes.get(sym)
            t_age_min = (now_et - t.timestamp).total_seconds() / 60 if t else None
            line = (
                f"{sym}: 最新成交 ${t.price:.2f} @ {t.timestamp.isoformat()} "
                f"({t_age_min:.0f}分钟前)" if t else f"{sym}: 无成交数据"
            )
            logger.info("行情诊断 %s", line)
            lines.append(line)
            if q:
                q_line = f"  报价 买${q.bid_price:.2f}/卖${q.ask_price:.2f} @ {q.timestamp.isoformat()}"
                logger.info("行情诊断 %s", q_line)
                lines.append(q_line)
    except Exception as e:
        logger.error("行情诊断失败: %s", e)
        lines.append(f"❌ 查询失败: {e}")

    msg = "\n".join(lines)
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("行情诊断结果发送失败: %s", e)


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
    for p in positions:
        logger.info("  持仓明细: %s ×%.0f 均价$%.2f 现价$%s 浮盈%+.0f(%+.1f%%)",
                     p["symbol"], p["qty"], p["avg_entry_price"],
                     f"{p['current_price']:.2f}" if p["current_price"] else "N/A",
                     p["unrealized_pl"], p["unrealized_plpc"])


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
    gemini_key     = config.GEMINI_API_KEY
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

    if scan_mode == "test_data":
        try:
            await _run_test_data(bot, chat_ids, alpaca_key, alpaca_secret)
        except Exception as e:
            logger.error("行情诊断失败: %s", e)
        return

    if scan_mode == "test_shadow":
        try:
            await _run_test_shadow(bot, chat_ids, gemini_key)
        except Exception as e:
            logger.error("影子模式诊断失败: %s", e)
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

    elif scan_mode == "portfolio":
        # 手动查询：随时获取当前 Alpaca 持仓快报，不等盘后 review，纯只读不触发交易
        try:
            await _sync_portfolio(bot, chat_ids, alpaca_key, alpaca_secret)
        except Exception as e:
            logger.error("持仓查询失败: %s", e)

    elif scan_mode == "review_force":
        # 手动补发：跳过日期检查，重发 predictions.json 中当前 scan_date 的复盘消息
        # （用于复盘消息因故发送失败后，修复问题后手动重发，不触发扫描/下单）
        try:
            await _run_review(bot, chat_ids, anthropic_key, finnhub_client,
                              strict_date_check=False)
        except Exception as e:
            logger.error("补发复盘失败: %s", e)

    elif scan_mode == "extended_watch":
        # 盘前/盘后哨兵：检测异动、按需触发AI分析、扩展时段裸单入场
        try:
            await _run_extended_watch(config, bot, chat_ids, finnhub_client, anthropic_key,
                                      alpaca_key, alpaca_secret)
        except Exception as e:
            logger.error("盘前盘后哨兵失败: %s", e)

    elif scan_mode == "scan":
        # 常规时段开盘：先给盘前/盘后裸单成交的仓位补挂止损止盈保护
        try:
            await _attach_pending_protection(bot, chat_ids, alpaca_key, alpaca_secret)
        except Exception as e:
            logger.error("补挂保护失败: %s", e)
        today_preds = await _run_scan(config, bot, chat_ids, finnhub_client, anthropic_key, gemini_key)
        # 盘前执行：信号生成后立即下单
        try:
            await _run_execution(bot, chat_ids, alpaca_key, alpaca_secret,
                                 today_preds or [], config)
        except Exception as e:
            logger.error("Alpaca 执行失败: %s", e)

    else:
        # all-in-one 模式（手动触发且没填scan_mode时落到这支）
        try:
            await _run_review(bot, chat_ids, anthropic_key, finnhub_client,
                              strict_date_check=False)
        except Exception as e:
            logger.error("复盘失败: %s", e)
        # 跟 scan 分支一样，先给盘前/盘后裸单成交的仓位补挂止损止盈保护——
        # 漏了这步的话，手动触发all-in-one时留下的隔夜裸仓要等到下一次
        # 正常的scan分支才会被处理
        try:
            await _attach_pending_protection(bot, chat_ids, alpaca_key, alpaca_secret)
        except Exception as e:
            logger.error("补挂保护失败: %s", e)
        today_preds = await _run_scan(config, bot, chat_ids, finnhub_client, anthropic_key, gemini_key)
        try:
            await _run_execution(bot, chat_ids, alpaca_key, alpaca_secret,
                                 today_preds or [], config)
        except Exception as e:
            logger.error("Alpaca 执行失败: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
