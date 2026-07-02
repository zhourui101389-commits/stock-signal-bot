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

    # 如果现有文件是不同日期的数据（已含复盘结果），归入 history
    existing_date = existing.get("scan_date", "")
    if existing.get("predictions") and existing_date and existing_date != today:
        history.append({
            "scan_date":   existing_date,
            "predictions": existing["predictions"],
        })
        history = history[-_MAX_HISTORY_DAYS:]

    data = {
        "scan_date":   today,
        "predictions": predictions,
        "history":     history,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("已保存 %d 条预测记录（历史 %d 天）", len(predictions), len(history))


def load_predictions(path: str = _PRED_FILE) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("加载预测文件失败: %s", e)
        return {}


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
