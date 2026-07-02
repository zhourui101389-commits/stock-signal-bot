#!/usr/bin/env python
"""命令行快速运行深度报告，用法: python scripts/deep.py NVDA"""
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.data.moomoo_client import MoomooDataClient
from src.analysis.multi_timeframe import analyze_symbol
from src.notifications.formatter import format_deep_report


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def main() -> None:
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "NVDA"
    config = Config()
    client = MoomooDataClient(config.MOOMOO_HOST, config.MOOMOO_PORT)

    print(f"正在分析 {symbol}，请稍候（约15秒）...")
    signal = analyze_symbol(
        client, symbol,
        config.signals.get("lookback_days", 250),
        config.signals.get("lookback_weeks", 104),
        config.total_capital,
        config.max_position_pct,
    )
    deep = client.get_deep_report(symbol)
    html = format_deep_report(symbol, signal, deep)
    print(strip_html(html))


if __name__ == "__main__":
    main()
