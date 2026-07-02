"""格式化 Telegram HTML 消息。"""
import html as _html
import math
from datetime import datetime, timezone, timedelta
from src.analysis.signals import SignalResult

_ET = timezone(timedelta(hours=-4))   # UTC-4 (EDT 夏令时)

_CST = timezone(timedelta(hours=8))


def _v(val, fmt=".2f", prefix="$", na="N/A") -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return na
    return f"{prefix}{val:{fmt}}" if prefix else f"{val:{fmt}}"


def _pct(val, na="N/A") -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return na
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


def _m(val, na="N/A") -> str:
    """百万美元格式，带正负号"""
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return na
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1f}M"


def _scan_label() -> str:
    hour = datetime.now(_CST).hour
    if hour == 21:
        return "🌅 盘前分析"
    elif hour == 22:
        return "📊 盘中报告 · 开盘约30分钟"
    elif hour == 23:
        return "📊 盘中报告 · 开盘约90分钟"
    else:
        return "📊 盘中报告 · 开盘约150分钟"


def _stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n) if 0 < n <= 5 else ""


def _flow_bar(net: float) -> str:
    """简单方向箭头"""
    if math.isnan(net):
        return ""
    return "🟢" if net > 0 else "🔴"


def _format_action_guide(result, price: float, pinned: bool,
                         total_capital: float = 50000, currency: str = "AUD",
                         ai_result: dict = None) -> str:
    """根据仓位分层和信号生成具体操作指引。"""
    if math.isnan(price) or price <= 0:
        return ""

    ai_stop   = (ai_result or {}).get("stop_loss")
    ai_target = (ai_result or {}).get("target_price")
    ai_action = (ai_result or {}).get("action", "")

    tier      = getattr(result, "tier", "swing")
    # AI 操作建议驱动操作指引方向，保证上下一致
    if ai_action in ("积极买入", "谨慎买入"):
        direction = "BUY"
    elif ai_action in ("减仓", "回避"):
        direction = "SELL"
    elif ai_result and ai_action == "持有观望":
        direction = "NEUTRAL"   # AI 说观望则不展示买入细节
    else:
        direction = result.direction
    strength  = result.strength
    atr_pct   = result.atr_pct if not math.isnan(result.atr_pct) else 3.0
    vr        = result.volume_ratio
    wt        = result.weekly_trend
    cur       = currency  # 货币符号

    def _money(amt: float) -> str:
        return f"{cur} {amt:,.0f}"

    # ── 止损率 / 止盈率 / 仓位比例 ───────────────────────────────────
    if tier == "core":
        sl_pct, tp1, tp2  = 0.08, 0.10, 0.20
        alloc_pct         = 0.20          # 核心仓目标 20% 总资金
        batch_amt         = round(total_capital * alloc_pct / 3 / 100) * 100
        batch_label       = f"核心仓 · 第1批（共3批，目标 {alloc_pct*100:.0f}%）"
        entry_note        = "次日开盘30分钟后 限价买入"
        exit_note_last    = "剩余跌破10日线清仓"
    elif tier == "speculative":
        sl_pct, tp1, tp2  = 0.04, 0.05, 0.08
        alloc_pct         = 0.05          # 投机仓 5%
        batch_amt         = round(total_capital * alloc_pct / 100) * 100
        batch_label       = f"投机仓（上限 {alloc_pct*100:.0f}%，当日信号专用）"
        entry_note        = "今日盘中开盘30分钟后 确认涨势入场"
        exit_note_last    = "不过夜超3天"
    else:  # swing
        sl_pct, tp1, tp2  = 0.06, 0.10, 0.18
        alloc_pct         = 0.08          # 机动仓 8%
        batch_amt         = round(total_capital * alloc_pct / 100) * 100
        batch_label       = f"机动仓（上限 {alloc_pct*100:.0f}%）"
        entry_note        = "次日开盘30分钟后 限价买入"
        exit_note_last    = "剩余止损-6%清仓"

    # SELL 方向
    if direction == "SELL":
        entry_note  = "有持仓：考虑减仓或止盈离场"
        batch_amt   = 0
        batch_label = "—"

    # ── 价格计算（USD，股票标价）─────────────────────────────────────
    sl_price  = price * (1 - sl_pct)
    tp1_price = price * (1 + tp1)
    tp2_price = price * (1 + tp2)

    # ── 入场条件检查 ────────────────────────────────────────────────
    checks = []
    if not math.isnan(vr):
        ok = vr >= 1.3
        checks.append(f"{'✅' if ok else '⚠️'} 量比 {vr:.1f}x {'（有量）' if ok else '（量能偏弱，慎追）'}")
    ok_str = strength >= 40
    checks.append(f"{'✅' if ok_str else '⚠️'} 信号强度 {strength}{'（达标）' if ok_str else '（偏弱，轻仓）'}")
    if wt == "BULLISH":
        checks.append("✅ 周线趋势看涨")
    elif wt == "BEARISH":
        checks.append("⚠️ 周线趋势偏空，谨慎做多")
    atr_sl = atr_pct * 2
    if atr_sl > sl_pct * 100:
        checks.append(f"⚠️ 2×ATR({atr_sl:.1f}%) > 止损({sl_pct*100:.0f}%)，波动偏大")
    if result.next_earnings_date:
        from datetime import date
        try:
            days = (date.fromisoformat(result.next_earnings_date) - date.today()).days
            if 0 <= days <= 3:
                checks.append(f"🔴 财报 {days} 天内，建议财报后再建仓")
            elif days <= 7:
                checks.append(f"⚠️ 财报 {days} 天内，建议轻仓（半仓以内）")
        except ValueError:
            pass

    # ── 拼合输出 ────────────────────────────────────────────────────
    lines = ["\n<b>🎯 操作指引</b>"]
    if direction == "BUY" and batch_amt > 0:
        lines.append(f"  动作: {entry_note}")
        lines.append(f"  仓位: <b>{_money(batch_amt)}</b>（{batch_label}）")
        # 优先使用 AI 给出的止损价
        if ai_stop:
            sl_pct_actual = (price - ai_stop) / price * 100 if price > 0 else 0
            lines.append(f"  止损: <b>USD {ai_stop:.2f}</b>（-{sl_pct_actual:.1f}%）")
        else:
            lines.append(f"  止损: <b>USD {sl_price:.2f}</b>（-{sl_pct*100:.0f}% 硬止损）")
        # 优先使用 AI 给出的目标价
        if ai_target:
            upside = (ai_target - price) / price * 100 if price > 0 else 0
            lines.append(f"  目标价: <b>USD {ai_target:.2f}</b>（{upside:+.1f}%）")
        else:
            lines.append(f"  止盈1: USD {tp1_price:.2f}（+{tp1*100:.0f}% 减1/3）")
            lines.append(f"  止盈2: USD {tp2_price:.2f}（+{tp2*100:.0f}% 再减1/3）")
            lines.append(f"  止盈3: {exit_note_last}")
    elif direction == "SELL":
        lines.append(f"  动作: {entry_note}")
        if ai_stop:
            lines.append(f"  止损参考: USD {ai_stop:.2f}")
    else:
        lines.append("  动作: 观望，等待更强信号")

    if checks:
        lines.append("\n  " + "\n  ".join(checks))

    return "\n".join(lines) + "\n"


def format_signal_message(result: SignalResult, pinned: bool = False, ai_result: dict = None) -> str:
    price = result.current_price if not math.isnan(result.current_price) else result.close_price

    # ── 统一操作建议：AI 优先，无 AI 则降级用技术方向 ──
    if ai_result:
        ai_action_str = ai_result.get("action", "持有观望")
        conviction    = ai_result.get("conviction", "中")
        horizon       = ai_result.get("horizon", "3-5天")
        verdict       = _html.escape(ai_result.get("verdict", ""))
        final_dir     = ai_result.get("final_direction", "中性")
        tech_confirmed = ai_result.get("tech_confirmed", True)
        override_reason = _html.escape(str(ai_result.get("override_reason") or ""))
        analysis      = _html.escape(ai_result.get("analysis", ""))
        bull_case     = _html.escape(ai_result.get("bull_case", ""))
        bear_case     = _html.escape(ai_result.get("bear_case", ""))
        catalyst      = _html.escape(str(ai_result.get("catalyst") or "")) or None

        dir_emoji = {"看多": "🟢", "看空": "🔴", "中性": "⚪"}.get(final_dir, "⚪")
        conv_emoji = {"高": "🔥", "中": "✅", "低": "💡"}.get(conviction, "💡")

        if ai_action_str in ("积极买入", "谨慎买入"):
            action = f"✅ {ai_action_str}"
            pos_line = (
                f"建议仓位: <b>{result.position_shares} 股</b>（约 {_v(result.position_usd)}）"
                if result.position_shares > 0 else "建议仓位: 少量试仓"
            )
        elif ai_action_str in ("减仓", "回避"):
            action = f"❌ {ai_action_str}"
            pos_line = "建议: 考虑减仓或止盈离场"
        else:
            action = "⏸ 持有观望"
            pos_line = "建议: 暂不操作，等待更强信号"
    else:
        # 无 AI 时降级
        final_dir = {"BUY": "看多", "SELL": "看空"}.get(result.direction, "中性")
        dir_emoji = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}.get(result.direction, "⚪")
        conviction = "中"
        conv_emoji = "💡"
        horizon = "3-5天"
        verdict = ""
        tech_confirmed = True
        override_reason = ""
        analysis = ""
        bull_case = ""
        bear_case = ""
        catalyst = None

        if result.direction == "BUY":
            action = "✅ 建议买入"
            pos_line = (
                f"建议仓位: <b>{result.position_shares} 股</b>（约 {_v(result.position_usd)}）"
                if result.position_shares > 0 else "建议仓位: 少量试仓"
            )
        elif result.direction == "SELL":
            action = "❌ 建议卖出/减仓"
            pos_line = "建议: 考虑减仓或止盈离场"
        else:
            action = "⏸ 观望"
            pos_line = "建议: 暂不操作"

    # ── 今日涨幅预估 ──
    gain_raw  = result.estimated_gain_raw_pct
    gain_cons = result.estimated_gain_pct
    if result.direction != "NEUTRAL" and gain_raw != 0.0:
        est_line = f"今日预估: 技术满值 <b>{_pct(gain_raw)}</b>  →  保守 <b>{_pct(gain_cons)}</b>"
        atr_note = f"  （日内典型波幅 {_pct(result.atr_pct, 'N/A')}，保守值 = 满值 × 55%）"
    else:
        est_line = ""
        atr_note = ""

    # ── 财报提醒 ──
    earnings_warn = ""
    if result.next_earnings_date:
        from datetime import date
        try:
            nxt = date.fromisoformat(result.next_earnings_date)
            days_left = (nxt - date.today()).days
            if days_left <= 7:
                earnings_warn = f"\n⚠️ <b>财报预警</b>: {result.next_earnings_date} 还有 {days_left} 天"
            elif days_left <= 30:
                earnings_warn = f"\n📅 下次财报: {result.next_earnings_date}（{days_left} 天后）"
        except ValueError:
            pass

    # ── 实时行情 ──
    today_chg = _pct(result.today_change_pct)
    pre_chg   = _pct(result.pre_change_pct)
    vr        = _v(result.volume_ratio, ".2f", "", "N/A")
    pe        = _v(result.pe_ttm, ".1f", "", "N/A")
    trend_map = {"BULLISH": "📈 看涨", "BEARISH": "📉 看跌", "NEUTRAL": "➡️ 中性"}
    trend = trend_map.get(result.weekly_trend, result.weekly_trend)

    # ── 关键价位 ──
    levels = []
    if not math.isnan(result.ma200):
        delta = (price - result.ma200) / result.ma200 * 100
        levels.append(f"MA200 {_v(result.ma200)}（{_pct(delta)}）")
    if not math.isnan(result.bb_upper):
        levels.append(f"布林上轨 {_v(result.bb_upper)}")
    if not math.isnan(result.bb_lower):
        levels.append(f"布林下轨 {_v(result.bb_lower)}")
    if not math.isnan(result.week52_high):
        levels.append(f"52W 高/低 {_v(result.week52_high)} / {_v(result.week52_low)}")
    levels_str = "\n".join(f"  {l}" for l in levels) if levels else "  N/A"

    # ── 资金动向 ──
    flow_parts = []
    if not math.isnan(result.net_super_flow):
        flow_parts.append(
            f"  超大单 {_flow_bar(result.net_super_flow)} <b>{_m(result.net_super_flow)}</b>"
            f"  大单 {_flow_bar(result.net_big_flow)} {_m(result.net_big_flow)}"
            f"  散单 {_flow_bar(result.net_small_flow)} {_m(result.net_small_flow)}"
        )
        inst_net = (result.net_super_flow or 0) + (result.net_big_flow or 0)
        flow_parts.append(
            f"  机构净合计: <b>{_m(inst_net)}</b>（超大+大单）"
        )
    flow_block = "\n".join(flow_parts) if flow_parts else "  暂无数据"

    # ── 估值水位 ──
    val_parts = []
    if not math.isnan(result.pe_ttm):
        val_parts.append(
            f"  PE(TTM) <b>{_v(result.pe_ttm, '.1f', '')}</b>"
            + (f"  5年均值 {_v(result.pe_5yr_avg, '.1f', '')}" if not math.isnan(result.pe_5yr_avg) else "")
            + (f"  百分位 <b>{_v(result.pe_percentile, '.1f', '')}%</b>" if not math.isnan(result.pe_percentile) else "")
        )
    if not math.isnan(result.pe_forward):
        val_parts.append(f"  远期PE {_v(result.pe_forward, '.1f', '')}")
    if not math.isnan(result.morningstar_fair_value) and result.morningstar_stars > 0:
        ms_upside = (result.morningstar_fair_value - price) / price * 100 if price > 0 else float("nan")
        stars_str = _stars(result.morningstar_stars)
        val_parts.append(
            f"  Morningstar {stars_str}  公允价 <b>{_v(result.morningstar_fair_value)}</b>"
            + (f"  空间 {_pct(ms_upside)}" if not math.isnan(ms_upside) else "")
        )
    val_block = "\n".join(val_parts) if val_parts else "  暂无数据"

    # ── 做空数据 ──
    short_parts = []
    if not math.isnan(result.short_pct):
        short_parts.append(
            f"  做空比例 <b>{_v(result.short_pct, '.2f', '')}%</b>"
            f"  回补天数 {_v(result.days_to_cover, '.1f', '')}"
        )
    short_block = "\n".join(short_parts) if short_parts else "  暂无数据"

    # ── 分析师共识 ──
    analyst_lines = []
    if not math.isnan(result.analyst_buy_pct):
        hold_pct = 100 - result.analyst_buy_pct
        bar_buy  = "█" * int(result.analyst_buy_pct / 10 + 0.5)
        bar_hold = "░" * max(0, 10 - int(result.analyst_buy_pct / 10 + 0.5))
        analyst_lines.append(
            f"  买入 {result.analyst_buy_pct:.0f}% {bar_buy}{bar_hold} {hold_pct:.0f}%"
            f"  （{result.analyst_total} 位分析师）"
        )
    if not math.isnan(result.analyst_target_avg):
        upside = (result.analyst_target_avg - price) / price * 100 if price > 0 else float("nan")
        analyst_lines.append(
            f"  均价目标 <b>{_v(result.analyst_target_avg)}</b>"
            f"  最高 {_v(result.analyst_target_high)}"
            + (f"  空间 {_pct(upside)}" if not math.isnan(upside) else "")
        )
    analyst_block = "\n".join(analyst_lines) if analyst_lines else "  暂无数据"

    # ── 内部人士交易 ──
    insider_lines = []
    for t in result.insider_trades:
        shares = t.get("shares", 0)
        tx_action = t.get("action", "")
        tx_label = "🟢 买入" if tx_action in ("买入", "BUY") else "🔴 卖出"
        insider_lines.append(
            f"  {tx_label} {abs(shares):,}股  {t.get('name','')} ({t.get('title','')})  {t.get('date','')}"
        )
    insider_block = "\n".join(insider_lines) if insider_lines else "  近期无记录"

    # ── 期权大单异动 ──
    opt_block = ""
    if result.options_unusual:
        lines = [l.strip() for l in result.options_unusual.split("\n") if l.strip()]
        shown = lines[1:4] if len(lines) > 1 else lines  # 跳过标题行，最多3条
        opt_block = (
            f"\n<b>⚡ 期权大单异动（近7日）</b>\n"
            + "\n".join(f"  {l}" for l in shown)
            + "\n"
        )

    # ── 财务/技术异动 ──
    unusual_parts = []
    if result.financial_unusual:
        unusual_parts.append(f"  财务: {result.financial_unusual[:100]}")
    if result.technical_unusual:
        unusual_parts.append(f"  技术: {result.technical_unusual[:100]}")
    unusual_block = ("\n<b>🚨 市场异动</b>\n" + "\n".join(unusual_parts) + "\n") if unusual_parts else ""

    # ── Morningstar 最新观点 ──
    ms_block = ""
    if result.morningstar_note_title:
        ms_block = f"\n<b>📰 Morningstar 最新观点</b>\n  {result.morningstar_note_title}\n"
        if result.morningstar_bull:
            ms_block += "  多方: " + result.morningstar_bull[0][:80] + "\n"
        if result.morningstar_bear:
            ms_block += "  空方: " + result.morningstar_bear[0][:80] + "\n"

    # ── 技术信号依据 ──
    reasons_html = "\n".join(f"  • {r}" for r in result.reasons)

    ts = datetime.now(_CST).strftime("%H:%M")
    rsi_str = _v(result.rsi, ".1f", "", "N/A")
    pin_tag = "📌 " if pinned else ""

    # ── 综合研判板块 ──
    tech_dir_cn = {"BUY": "看多", "SELL": "看空", "NEUTRAL": "中性"}.get(result.direction, result.direction)
    if ai_result:
        confirm_tag = "✅ AI确认" if tech_confirmed else f"⚠️ AI推翻：{override_reason}"
        research_lines = [
            f"\n<b>🧠 综合研判</b>  技术: {tech_dir_cn} 强度{result.strength}  {confirm_tag}",
            f"  {analysis}",
        ]
        if bull_case:
            research_lines.append(f"  🟢 多方: {bull_case}")
        if bear_case:
            research_lines.append(f"  🔴 空方: {bear_case}")
        if catalyst:
            research_lines.append(f"  ⚡ 催化剂: {catalyst}")
        research_block = "\n".join(research_lines) + "\n"
    else:
        research_block = ""

    # ── 操作指引 ──────────────────────────────────────────────────────
    total_capital = getattr(result, "_total_capital", 50000)
    currency      = getattr(result, "_currency", "AUD")
    action_block  = _format_action_guide(result, price, pinned, total_capital, currency, ai_result)

    # ── 标题行（带综合方向） ──
    if ai_result and verdict:
        header_verdict = f"\n<b>{verdict}</b>"
        meta_line = f"置信度: {conv_emoji} {conviction}  周期: {horizon}  {dir_emoji} {final_dir}"
    else:
        header_verdict = ""
        meta_line = f"技术强度: {result.strength}/100"

    return (
        f"<b>{pin_tag}{_scan_label()} — {result.symbol}</b>\n"
        f"{'─' * 30}\n"
        f"今日操作: <b>{action}</b>\n"
        f"{meta_line}\n"
        f"{pos_line}\n"
        + (f"{header_verdict}\n" if header_verdict else "")
        + (f"{est_line}\n{atr_note}\n" if est_line else "")
        + f"\n"
        f"<b>📡 实时行情</b>{earnings_warn}\n"
        f"  当前价: <b>{_v(price)}</b>  今日: {today_chg}\n"
        f"  盘前: {pre_chg}  量比: {vr}x  RSI: {rsi_str}\n"
        f"  PE(TTM): {pe}  周线趋势: {trend}\n"
        f"{research_block}"
        f"\n"
        f"<b>📍 关键价位</b>\n"
        f"{levels_str}\n"
        f"\n"
        f"<b>💰 今日资金动向</b>\n"
        f"{flow_block}\n"
        f"\n"
        f"<b>📊 估值水位</b>\n"
        f"{val_block}\n"
        f"\n"
        f"<b>📉 做空指标</b>\n"
        f"{short_block}\n"
        f"\n"
        f"<b>🏦 华尔街分析师共识</b>\n"
        f"{analyst_block}\n"
        f"\n"
        f"<b>👔 内部人士近期交易</b>\n"
        f"{insider_block}\n"
        f"{opt_block}"
        f"{unusual_block}"
        f"{ms_block}"
        f"\n"
        f"<b>📋 技术信号依据</b>\n"
        f"{reasons_html}\n"
        f"{action_block}"
        f"\n"
        f"<i>⏱ {ts} CST  ⚠️ 综合分析仅供参考，盈亏自负</i>"
    )


def _vol_label(vr: float) -> str:
    if math.isnan(vr):   return "N/A"
    if vr >= 2.0:        return f"{vr:.2f}x ⚡超大量"
    if vr >= 1.5:        return f"{vr:.2f}x 📈放量"
    if vr >= 0.8:        return f"{vr:.2f}x 正常"
    return               f"{vr:.2f}x 📉缩量"


def _bab_label(bab: float) -> str:
    if math.isnan(bab):  return "N/A"
    sign = "🟢+" if bab > 0 else "🔴"
    if abs(bab) >= 30:   qual = "强势" if bab > 0 else "强势卖压"
    elif abs(bab) >= 10: qual = "偏多" if bab > 0 else "偏空"
    else:                qual = "均衡"
    return f"{sign}{bab:.0f} ({qual})"


def _flow_verdict(net_super: float, net_big: float) -> str:
    """根据超大单+大单合计给出机构资金方向标签。"""
    if math.isnan(net_super) or math.isnan(net_big):
        return ""
    total = net_super + net_big
    icon = "🟢" if total > 0 else "🔴"
    label = "净买入" if total > 0 else "净卖出"
    m = abs(total) / 1e6
    if m >= 100:   mag = "大幅"
    elif m >= 30:  mag = "明显"
    else:          mag = ""
    return f"{icon} 机构{mag}{label} {abs(total)/1e6:.0f}M"


def _signal_verdict(direction: str, cur_chg: float, atr_pct: float,
                    net_super: float, net_big: float, est_cons: float) -> str:
    """给出一句综合判断。"""
    nan = math.isnan
    chg_ok = not nan(cur_chg)
    inst_ok = not (nan(net_super) or nan(net_big))
    inst_net = (net_super + net_big) if inst_ok else 0.0
    inst_pos = inst_net > 0

    if direction == "BUY":
        if chg_ok and cur_chg > 0:
            if inst_ok and not inst_pos:
                verdict = "⚠️ 价格上涨但机构悄悄流出，注意高位止盈"
            elif not nan(atr_pct) and atr_pct > 0 and abs(cur_chg) > atr_pct * 1.1:
                verdict = "🔥 涨幅已超ATR，技术上短期透支，谨慎追高"
            elif inst_ok and inst_pos:
                verdict = "✅ 信号延续，主力买入支撑，可持有观察"
            else:
                verdict = "✅ 价格符合预期，注意量能变化"
        elif chg_ok and cur_chg < 0:
            if inst_ok and inst_pos:
                verdict = "⚠️ 价格暂时回调但机构买入护盘，等待企稳"
            else:
                verdict = "❌ 方向逆转，跌破盘前预期，考虑止损"
        else:
            verdict = "⏳ 行情数据待更新"
    else:  # SELL
        if chg_ok and cur_chg < 0:
            if inst_ok and inst_pos:
                verdict = "⚠️ 价格下跌但机构逆向买入，空头信号可能减弱"
            else:
                verdict = "✅ 信号延续，机构持续出货，观望为主"
        elif chg_ok and cur_chg > 0:
            verdict = "❌ 空头逆转拉升，止损信号，注意风险"
        else:
            verdict = "⏳ 行情数据待更新"
    return verdict


def format_followup_message(hour: int, updates: list[dict]) -> str:
    """
    22:00/23:00/00:00 追踪报告，包含：
      - 实际涨跌 vs 9点预估 + ATR进度
      - 机构资金方向（超大单+大单合计）
      - 量比解读、买卖盘情绪
      - 综合判断一句话
    updates: [{"symbol", "direction", "strength", "estimated_gain_pct",
               "estimated_gain_raw_pct", "atr_pct", "initial_price",
               "quote": {...}, "flow": {...}}]
    """
    if not updates:
        return ""

    label_map = {22: "开盘约 30 分钟", 23: "开盘约 90 分钟", 0: "开盘约 150 分钟"}
    label = label_map.get(hour, f"{hour}:00")
    ts = datetime.now(_CST).strftime("%H:%M")

    lines = [
        f"<b>📡 盘中追踪 · {label}</b>  <i>{ts} CST</i>",
        "─" * 32,
    ]

    for u in updates:
        sym       = u["symbol"]
        direction = u["direction"]
        strength  = u.get("strength", 0)
        est_cons  = u.get("estimated_gain_pct", 0.0)
        est_raw   = u.get("estimated_gain_raw_pct", 0.0)
        atr_pct   = u.get("atr_pct", float("nan"))
        quote     = u.get("quote", {})
        flow      = u.get("flow", {})

        cur_chg   = quote.get("today_change_pct", float("nan"))
        cur_price = quote.get("latest_price", float("nan"))
        bid_ask   = quote.get("bid_ask_ratio", float("nan"))
        vol_ratio = quote.get("volume_ratio", float("nan"))
        amplitude = quote.get("amplitude", float("nan"))

        net_super = flow.get("net_super", float("nan"))
        net_big   = flow.get("net_big",   float("nan"))
        net_small = flow.get("net_small", float("nan"))

        # ── 标题行 ──────────────────────────────────
        dir_icon  = "📈" if direction == "BUY" else "📉"
        chg_icon  = "▲" if (not math.isnan(cur_chg) and cur_chg >= 0) else "▼"
        chg_str   = _pct(cur_chg) if not math.isnan(cur_chg) else "N/A"
        price_str = _v(cur_price) if not math.isnan(cur_price) else "N/A"
        lines.append(
            f"\n{dir_icon} <b>{sym}</b>  {chg_icon} <b>{chg_str}</b>  {price_str}"
            f"  <i>({direction} 强度 {strength})</i>"
        )

        # ── 预估 vs 实际 ─────────────────────────────
        if est_raw != 0:
            if not math.isnan(cur_chg):
                vs     = cur_chg - est_cons
                vs_tag = f"{'超预期' if (direction=='BUY' and vs>0) or (direction=='SELL' and vs<0) else '未达预期'} {'+' if vs>0 else ''}{vs:.1f}%"
            else:
                vs_tag = ""
            lines.append(
                f"  9点预估: 保守 {_pct(est_cons)} / 技术 {_pct(est_raw)}"
                + (f"  →  {vs_tag}" if vs_tag else "")
            )

        # ── ATR 进度 ──────────────────────────────────
        if not math.isnan(cur_chg) and not math.isnan(atr_pct) and atr_pct > 0:
            consumed = abs(cur_chg) / atr_pct * 100
            if consumed > 110:
                atr_note = f"  ATR进度 {consumed:.0f}%（已超ATR，动能过热）"
            elif consumed > 75:
                atr_note = f"  ATR进度 {consumed:.0f}%（接近极限，谨慎追价）"
            else:
                atr_note = f"  ATR进度 {consumed:.0f}%（仍有波动空间）"
            lines.append(atr_note)

        # ── 资金流向 ──────────────────────────────────
        flow_parts = []
        if not math.isnan(net_super):
            flow_parts.append(f"超大单 {'🟢+' if net_super>=0 else '🔴'}{net_super/1e6:.0f}M")
        if not math.isnan(net_big):
            flow_parts.append(f"大单 {'🟢+' if net_big>=0 else '🔴'}{net_big/1e6:.0f}M")
        if not math.isnan(net_small):
            flow_parts.append(f"散单 {'🟢+' if net_small>=0 else '🔴'}{net_small/1e6:.0f}M")

        if flow_parts:
            fv = _flow_verdict(net_super, net_big)
            lines.append(f"  💰 {' | '.join(flow_parts)}")
            if fv:
                lines.append(f"     → {fv}")

        # ── 量比 + 买卖盘 ──────────────────────────────
        lines.append(
            f"  📊 量比 {_vol_label(vol_ratio)}  |  买卖盘 {_bab_label(bid_ask)}"
        )

        # ── 综合判断 ──────────────────────────────────
        verdict = _signal_verdict(direction, cur_chg, atr_pct, net_super, net_big, est_cons)
        lines.append(f"  📝 {verdict}")

    lines.append(f"\n<i>⚠️ 数据来自 Moomoo 实时快照，仅供参考</i>")
    return "\n".join(lines)


# ── 宏观经济日历 ───────────────────────────────────────────────────────────────

def format_economic_calendar(events: list[dict]) -> str:
    """
    格式化 Finnhub 经济日历消息。
    每个 event 含 cn_name / comment（由 economic_calendar.py 注入）。
    """
    if not events:
        return ""

    now      = datetime.now(_CST)
    today    = now.strftime("%Y-%m-%d")
    yest     = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    tom      = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    tom2     = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    def _date_label(d: str) -> str:
        if d == today: return "📌 今天"
        if d == yest:  return "🕐 昨天"
        if d == tom:   return "🗓 明天"
        if d == tom2:  return "🗓 后天"
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            wday = ["周一","周二","周三","周四","周五","周六","周日"][dt.weekday()]
            return f"🗓 {dt.month}月{dt.day}日({wday})"
        except Exception:
            return f"🗓 {d}"

    def _impact_icon(impact: str) -> str:
        i = (impact or "").lower()
        return "🔴" if i == "high" else ("🟡" if i == "medium" else "⚪")

    def _fmt(v, unit: str) -> str:
        if v is None:
            return "—"
        try:
            f = float(v)
            u = (unit or "").strip()
            if u == "%":
                s = "+" if f >= 0 else ""
                return f"{s}{f:.2f}%"
            if u in ("K", "M", "B"):
                return f"{f:.1f}{u}"
            return f"{f:+.2f}" if abs(f) < 1000 else f"{f:.0f}"
        except (TypeError, ValueError):
            return str(v)

    lines = ["<b>📅 美国宏观日历</b>", "─" * 30]
    prev_date = None

    for ev in events:
        ev_time = ev.get("time", "")
        ev_date = ev_time[:10] if ev_time else ""

        # 日期分组标题
        if ev_date != prev_date:
            prev_date = ev_date
            lines.append(f"\n<b>{_date_label(ev_date)}</b>")

        # 时间（ET）
        try:
            dt_et = datetime.strptime(ev_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ET)
            time_str = dt_et.strftime("%H:%M ET")
        except Exception:
            time_str = "全天"

        impact   = ev.get("impact", "")
        cn_name  = ev.get("cn_name") or ev.get("event", "")[:28]
        actual   = ev.get("actual")
        est      = ev.get("estimate")
        prev     = ev.get("prev")
        unit     = ev.get("unit", "")
        comment  = ev.get("comment", "")

        icon = _impact_icon(impact)

        # 事件标题行
        has_actual = actual is not None
        status = " ✅已公布" if has_actual else ""
        lines.append(f"{icon} <b>{cn_name}</b>  <i>{time_str}{status}</i>")

        # 数值行：前值 | 预估 | 实际
        val_parts = []
        if prev is not None:
            val_parts.append(f"前值 {_fmt(prev, unit)}")
        if est is not None:
            val_parts.append(f"预估 {_fmt(est, unit)}")
        if has_actual:
            val_parts.append(f"<b>实际 {_fmt(actual, unit)}</b>")
        if val_parts:
            lines.append(f"  {'  |  '.join(val_parts)}")

        # 预判/点评
        if comment:
            lines.append(f"  💬 {comment}")

    lines.append(f"\n<i>数据来源: Finnhub · 时间为 ET 东部时间</i>")
    return "\n".join(lines)


def format_deep_report(symbol: str, signal: SignalResult, deep: dict) -> str:
    """生成 /deep SYMBOL 的完整深度报告消息。"""
    price = signal.current_price if not math.isnan(signal.current_price) else signal.close_price
    ts = datetime.now(_CST).strftime("%Y-%m-%d %H:%M")

    lines = [f"<b>📘 {symbol} 深度报告</b>  <i>{ts} CST</i>"]
    lines.append("─" * 32)

    # ── 财报历史涨跌 ──
    history = signal.earnings_history or deep.get("earnings_history", [])
    if history:
        lines.append("\n<b>📈 历史财报日涨跌</b>  <i>（FY=财年，非自然年；报告日=发布当天）</i>")
        for h in history[-6:]:
            mv = h.get("move", float("nan"))
            arrow = "▲" if mv >= 0 else "▼"
            sign  = "+" if mv >= 0 else ""
            lines.append(f"  FY{h['period']}  报告日 {h['date']}  {arrow} {sign}{mv:.1f}%")
    if signal.next_earnings_date:
        from datetime import date as _date
        try:
            days_left = (_date.fromisoformat(signal.next_earnings_date) - _date.today()).days
            lines.append(f"  ➡ 下次财报预计: <b>{signal.next_earnings_date}</b>（{days_left} 天后）")
        except ValueError:
            lines.append(f"  ➡ 下次财报预计: {signal.next_earnings_date}")

    # ── 收入拆分 ──
    rb = deep.get("revenue_breakdown")
    if rb:
        lines.append(f"\n<b>💼 收入结构（{rb['period']}）</b>")
        for seg in rb.get("segments", []):
            bar = "█" * int(seg["pct"] / 10 + 0.5)
            lines.append(f"  {seg['name']}: {bar} {seg['pct']:.1f}%")

    # ── 分红 ──
    divs = deep.get("dividends", [])
    if divs:
        lines.append("\n<b>💵 近期分红记录</b>")
        for d in divs[:4]:
            lines.append(
                f"  {d.get('pub_date','')}  {d.get('statement','')}  "
                f"除权日 {d.get('ex_date','')}  到账 {d.get('dividend_payable_date','')}"
            )

    # ── 股东结构 ──
    holder_types = deep.get("holder_types", [])
    if holder_types:
        lines.append("\n<b>🏛 股东结构</b>")
        for h in holder_types[:6]:
            bar = "█" * max(1, int(h["pct"] / 5 + 0.5))
            lines.append(f"  {h['type']}: {bar} {h['pct']:.2f}%")

    # ── 机构持仓趋势 ──
    inst_trend = deep.get("inst_trend", [])
    if inst_trend:
        lines.append("\n<b>🏦 机构持仓变化趋势</b>")
        for it in inst_trend:
            chg_sign = "+" if it["pct_chg"] >= 0 else ""
            cnt_sign = "+" if it["count_chg"] >= 0 else ""
            lines.append(
                f"  {it['period']}  {it['count']}家机构（{cnt_sign}{it['count_chg']}）"
                f"  占股 {it['pct']:.2f}%（{chg_sign}{it['pct_chg']:.2f}%）"
            )

    # ── 运营效率 ──
    oe = deep.get("op_efficiency")
    if oe:
        lines.append(f"\n<b>⚙️ 运营效率（{oe['period']}）</b>")
        lines.append(f"  员工总数: {oe['employees']:,}人（YoY +{oe['emp_yoy']:.1f}%）")
        lines.append(f"  人均营收: ${oe['revenue_per_emp']/1e6:.2f}M")
        lines.append(f"  人均净利: ${oe['profit_per_emp']/1e6:.2f}M")

    # ── 分析师 + Morningstar ──
    if not math.isnan(signal.analyst_target_avg):
        lines.append(f"\n<b>🏦 分析师共识</b>")
        upside = (signal.analyst_target_avg - price) / price * 100 if price > 0 else float("nan")
        lines.append(
            f"  {signal.analyst_total}位分析师  买入 {signal.analyst_buy_pct:.0f}%"
            f"  目标价 {_v(signal.analyst_target_avg)}（空间 {_pct(upside)}）"
        )
    if not math.isnan(signal.morningstar_fair_value):
        ms_upside = (signal.morningstar_fair_value - price) / price * 100 if price > 0 else float("nan")
        lines.append(
            f"  Morningstar {_stars(signal.morningstar_stars)}"
            f"  公允价 {_v(signal.morningstar_fair_value)}（空间 {_pct(ms_upside)}）"
        )

    lines.append(f"\n<i>⚠️ 本报告数据来源 Moomoo/Morningstar，仅供参考，盈亏自负</i>")
    return "\n".join(lines)


def format_serenity_section(picks: dict) -> str:
    """
    格式化 Serenity (@aleabitoreddit) 供应链观点板块。
    picks: get_serenity_picks() 的返回值。
    """
    if not picks:
        return ""

    phase2 = picks.get("phase2", [])
    phase3 = picks.get("phase3", [])
    notes  = picks.get("notes", [])
    updated = picks.get("updated", "")
    source  = picks.get("source", "")

    src_tag = "🔴 兜底数据" if source == "fallback" else "🟢 今日抓取"

    lines = [
        f"\n<b>🎯 Serenity 供应链观点</b>  <i>(@aleabitoreddit · {src_tag})</i>",
        "─" * 30,
    ]

    if phase2:
        tickers = "  ".join(f"<code>${t}</code>" for t in phase2)
        lines.append(f"<b>Phase 2</b> 当前核心：{tickers}")
    if phase3:
        tickers = "  ".join(f"<code>${t}</code>" for t in phase3)
        lines.append(f"<b>Phase 3</b> 前瞻布局：{tickers}")

    if notes:
        lines.append("")
        for n in notes[:3]:
            lines.append(f"  📌 {n}")

    lines.append(
        f"\n<i>来源: semiconstocks.com  |  更新: {updated}"
        f"  |  ⚠️ 长线逻辑参考，非短线信号，盈亏自负</i>"
    )
    return "\n".join(lines)


def format_review_message(scan_date: str, reviewed: list[dict]) -> str:
    """
    格式化逐股复盘消息。
    复盘的目的是展示"预判逻辑 vs 市场实际"，结果会回传给明日 AI 参考，
    不生成操作建议——操作建议由明日扫描时 AI 综合历史判断给出。

    reviewed 每项: {symbol, final_direction, conviction, verdict,
                    entry_price, close_price, actual_pct,
                    target_price, stop_loss, correct, hit_target, hit_stop}
    correct: True=方向正确, False=方向错误, None=观望未持仓
    """
    if not reviewed:
        return ""

    right   = [r for r in reviewed if r.get("correct") is True]
    wrong   = [r for r in reviewed if r.get("correct") is False]
    neutral = [r for r in reviewed if r.get("correct") is None]

    def _stock_block(r: dict) -> list[str]:
        sym        = r["symbol"]
        direction  = r.get("final_direction", "中性")
        conviction = r.get("conviction", "")
        verdict    = r.get("verdict", "")
        apct       = r.get("actual_pct")
        entry      = r.get("entry_price")
        close      = r.get("close_price")
        hit_t      = r.get("hit_target", False)
        hit_s      = r.get("hit_stop", False)
        target     = r.get("target_price")

        if apct is None:
            return [f"<b>{sym}</b>  ❓ 无收盘价数据"]

        sign      = "+" if apct >= 0 else ""
        arrow     = "▲" if apct >= 0 else "▼"
        price_str = f"${entry:.2f}→${close:.2f}" if entry and close else ""
        tag       = " 🎯达目标" if hit_t else (" 🛑触止损" if hit_s else "")
        conv_str  = f"  置信度：{conviction}" if conviction else ""
        dir_icon  = {"看多": "📈", "看空": "📉", "中性": "⚪"}.get(direction, "")

        lines = [
            f"<b>{sym}</b>  {dir_icon}{direction}  {arrow}{sign}{apct:.2f}%  {price_str}{tag}"
        ]
        if verdict:
            lines.append(f"  预判依据：{_html.escape(verdict)}{conv_str}")
        if target and not hit_t:
            gap = (target - close) / close * 100 if close else None
            if gap is not None:
                gap_str = f"距目标${target:.2f}还差 {gap:+.1f}%" if apct >= 0 else f"目标${target:.2f}未触达"
                lines.append(f"  {gap_str}")
        return lines

    lines = [
        f"<b>📋 昨日复盘 — {scan_date}</b>",
        "─" * 28,
        "<i>以下数据已回传 AI，供今日分析参考</i>",
    ]

    if right:
        lines.append(f"\n<b>✅ 方向正确（{len(right)}只）</b>")
        for r in right:
            lines += [""] + _stock_block(r)

    if wrong:
        lines.append(f"\n<b>❌ 方向错误（{len(wrong)}只）</b>")
        for r in wrong:
            lines += [""] + _stock_block(r)

    if neutral:
        lines.append(f"\n<b>⚪ 观望未持仓（{len(neutral)}只）</b>")
        for r in neutral:
            sym  = r["symbol"]
            apct = r.get("actual_pct")
            if apct is not None:
                sign  = "+" if apct >= 0 else ""
                arrow = "▲" if apct >= 0 else "▼"
                lines.append(f"  {sym}  {arrow}{sign}{apct:.2f}%")
            else:
                lines.append(f"  {sym}  无数据")

    lines.append(f"\n{'─' * 28}")
    lines.append(f"<i>✅ {len(right)} 只方向正确  ❌ {len(wrong)} 只方向错误</i>")
    return "\n".join(lines)


def format_watchlist_message(symbols: list[str]) -> str:
    if not symbols:
        return "📋 自选股列表为空\n使用 /add SYMBOL 添加股票"
    lines = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(symbols))
    return f"📋 <b>当前自选股（{len(symbols)} 只）</b>\n\n{lines}\n\n使用 /add 或 /remove 管理"


def format_ai_analysis(symbol: str, price: float, ai: dict, tech_direction: str = "") -> str:
    """已废弃：AI 分析现已合并进 format_signal_message。保留此函数避免旧调用报错。"""
    return ""


def format_help_message() -> str:
    return (
        "<b>📊 美股信号助手</b>\n\n"
        "可用命令：\n"
        "  /watchlist — 查看自选股列表\n"
        "  /add AAPL — 添加股票\n"
        "  /remove AAPL — 删除股票\n"
        "  /analyze AAPL — 立即技术分析\n"
        "  /deep AAPL — 深度报告（财报/分红/股东/机构）\n"
        "  /status — 系统状态\n\n"
        "每晚 21:00 22:00 23:00 00:00（北京时间）自动分析并推送"
    )
