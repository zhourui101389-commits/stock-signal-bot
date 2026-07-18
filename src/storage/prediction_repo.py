"""
预测记录管理：保存每日扫描的预测，供复盘使用。
数据存为 JSON 文件，通过 GitHub Actions cache 跨 workflow 传递。
文件结构包含 history 数组，滚动保存最近 10 天的复盘结果（含 correct/actual_pct），
供 AI 分析时识别系统性判断偏差。
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_PRED_FILE = os.environ.get("PREDICTIONS_FILE", "/tmp/predictions.json")
_MAX_HISTORY_DAYS = 90


def save_predictions(
    predictions: list[dict],
    path: str = _PRED_FILE,
    scan_date: str = None,
) -> None:
    import datetime
    today = scan_date or str(datetime.date.today())

    existing = load_predictions(path)
    history  = list(existing.get("history", []))

    existing_date  = existing.get("scan_date", "")
    existing_preds = existing.get("predictions", [])

    if existing_preds and existing_date and existing_date != today:
        # 不同日期的旧数据（已含复盘结果），归入 history
        history.append({
            "scan_date":   existing_date,
            "predictions": existing_preds,
        })
        history = history[-_MAX_HISTORY_DAYS:]
        merged = predictions
    elif existing_preds and existing_date == today:
        # 同一天第二次扫描（如盘中复查）：按symbol合并而非整体覆盖，
        # 重新扫过的股票用新结果，没重新扫的股票保留原预测，不丢数据。
        # 但如果已有记录带 trigger_session（说明是盘前/盘后哨兵真实下单
        # 成交的记录，entry_price是Alpaca真实成交价），后面同一天常规扫描
        # 重新分析同一标的产生的新记录不能覆盖它——那笔真实交易的价格
        # 会被换成一个从未真正下单的分析结果，复盘/校准用的入场价就错了
        #
        # 同理：如果已有记录当天已经给出过真实买卖判断（action是积极买入/
        # 谨慎买入/减仓/回避这几个真实决策之一，很可能已经据此下单成交），
        # 后面同一天的重扫如果给出的是"持有观望"或没有AI结果（比如免费
        # 检查点/AI预算用尽降级），也不能覆盖掉——2026-07-08 MRVL就是这样
        # 丢的：13:10那次AI判断"看多"触发了真实买入，20:18同一天重扫AI
        # 转"中性"，把当天记录整个覆盖成"持有观望"，事后完全查不出这笔
        # 交易当时是怎么判断出来的。同一天两次都是真实决策(比如早上谨慎
        # 买入、下午改积极买入)才允许覆盖——那是判断真的更新了，不是记录
        # 被技术面兜底结果冲掉
        _TRADE_ACTIONS = {"积极买入", "谨慎买入", "减仓", "回避"}
        by_symbol = {p["symbol"]: p for p in existing_preds}
        for p in predictions:
            existing_p = by_symbol.get(p["symbol"])
            if existing_p and existing_p.get("trigger_session"):
                continue
            if (existing_p and existing_p.get("action") in _TRADE_ACTIONS
                    and p.get("action") not in _TRADE_ACTIONS):
                continue
            by_symbol[p["symbol"]] = p
        merged = list(by_symbol.values())
    else:
        merged = predictions

    # 从 existing 起手而不是新建一个只有3个键的dict——否则 last_full_scan_date/
    # circuit_breaker/extended_alerts 等其它顶层标记会被静默清空。2026-07-15
    # 就是被这个坑绊倒的：scan_free 补跑刚写完 last_full_scan_date，同一次
    # 运行里紧接着自己的免费扫描又调一次 save_predictions()，标记被抹掉，
    # 导致当天下午同一个补跑逻辑又重复触发了三次，白烧了120次AI调用。
    data = dict(existing)
    data["scan_date"]   = today
    data["predictions"] = merged
    data["history"]     = history
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("已保存 %d 条预测记录（历史 %d 天）", len(merged), len(history))


def load_predictions(path: str = _PRED_FILE) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("加载预测文件失败: %s", e)
        return {}


def save_raw(data: dict, path: str = _PRED_FILE) -> None:
    """直接写入完整 predictions 数据结构，用于更新多天复盘结果等原地修改场景。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("已写回更新后的预测文件")


def get_symbol_history(symbol: str, path: str = _PRED_FILE) -> list[dict]:
    """
    获取该股票所有历史预测记录（时间升序），供 AI 识别判断规律。
    包含 correct/actual_pct 字段（复盘后写入）。
    """
    data = load_predictions(path)
    results = []

    # 从 history 数组中按时间顺序收集
    for day in data.get("history", []):
        date = day.get("scan_date", "")
        for p in day.get("predictions", []):
            if p.get("symbol") == symbol:
                results.append({**p, "scan_date": date})

    # 当前文件的 predictions（昨日复盘后写回，含 correct/actual_pct）
    current_date = data.get("scan_date", "")
    for p in data.get("predictions", []):
        if p.get("symbol") == symbol:
            results.append({**p, "scan_date": current_date})

    return results
