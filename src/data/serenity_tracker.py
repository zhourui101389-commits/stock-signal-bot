"""
Serenity (@aleabitoreddit) 公开观点追踪
数据来源：semiconstocks.com（第三方公开追踪站）
"""
import logging
import re
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)
_CST = timezone(timedelta(hours=8))

_TRACKER_URL = "https://semiconstocks.com/"
_TIMEOUT = 10

# ── 已知持仓（网站抓取失败时的兜底数据，2026-06-11 公开信息）──────────────────
_FALLBACK: dict = {
    "phase2": ["AAOI", "AXTI", "LITE", "COHR"],
    "phase3": ["SIVE", "POET"],
    "notes": [
        "Phase 2 核心：光收发器供应链瓶颈（InP 基板 + CW 激光）",
        "Phase 3 前瞻：CPO 共封装光学，目标 2027-2028 爆发窗口",
        "YTD +3,612%（自报，含杠杆，未经审计）",
    ],
    "updated": "2026-06-11",
    "source": "fallback",
}


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _extract_tickers(text: str) -> list[str]:
    """提取文本中所有 $TICK 格式的股票代码，去重保序。"""
    found = re.findall(r"\$([A-Z]{1,5})\b", text)
    seen = set()
    out = []
    for t in found:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _extract_notes(text: str, max_lines: int = 4) -> list[str]:
    """提取有意义的文本片段（含 Phase / 瓶颈 / 关键词的行）。"""
    keywords = ("Phase", "chokepoint", "bottleneck", "InP", "CPO",
                 "YTD", "substrate", "laser", "optical", "AMD", "NVDA",
                 "confirmed", "supply", "shortage")
    # 排除只是网站自我描述的通用行
    skip_phrases = ("for retail", "AI supply-chain chokepoints, explained",
                    "Serenity Tracker", "@aleabitoreddit")
    notes = []
    for line in text.splitlines():
        line = line.strip()
        if len(line) < 30:
            continue
        if any(s in line for s in skip_phrases):
            continue
        if any(k in line for k in keywords):
            notes.append(line[:120])
        if len(notes) >= max_lines:
            break
    return notes


def get_serenity_picks() -> dict:
    """
    抓取 semiconstocks.com 上的 Serenity 最新公开持仓/观点。

    返回：
    {
        "phase2": ["AAOI", ...],
        "phase3": ["SIVE", ...],
        "all_tickers": ["AAOI", "AXTI", ...],
        "notes": ["...", ...],
        "updated": "2026-06-21",
        "source": "live" | "fallback",
    }
    """
    try:
        resp = requests.get(
            _TRACKER_URL,
            timeout=_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"},
        )
        resp.raise_for_status()
        text = _strip_tags(resp.text)

        tickers = _extract_tickers(text)

        if len(tickers) < 3:
            # 页面可能是 JS 渲染的，内容不足，使用兜底
            logger.warning("semiconstocks.com 返回 %d 个股票代码，不足，使用兜底数据", len(tickers))
            result = dict(_FALLBACK)
            result["source"] = "fallback"
            return result

        notes = _extract_notes(text)

        # 按 Phase 2 / Phase 3 分段（简单关键词定位）
        text_lower = text.lower()
        p2_idx = text_lower.find("phase 2")
        p3_idx = text_lower.find("phase 3")

        if p2_idx != -1 and p3_idx != -1 and p3_idx > p2_idx:
            phase2_text = text[p2_idx:p3_idx]
            phase3_text = text[p3_idx:p3_idx + 500]
        else:
            phase2_text = text
            phase3_text = ""

        phase2 = _extract_tickers(phase2_text) or _FALLBACK["phase2"]
        # phase3 去掉已在 phase2 中出现的标的
        phase2_set = set(phase2)
        phase3_raw = _extract_tickers(phase3_text) or _FALLBACK["phase3"]
        phase3 = [t for t in phase3_raw if t not in phase2_set]

        return {
            "phase2": phase2[:8],
            "phase3": phase3[:6],
            "all_tickers": tickers[:12],
            "notes": notes or _FALLBACK["notes"],
            "updated": datetime.now(_CST).strftime("%Y-%m-%d"),
            "source": "live",
        }

    except Exception as exc:
        logger.error("抓取 Serenity Tracker 失败: %s，使用兜底数据", exc)
        result = dict(_FALLBACK)
        result["source"] = "fallback"
        return result
