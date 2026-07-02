"""
预测记录管理：保存每日扫描的预测，供复盘使用。
数据存为 JSON 文件，通过 GitHub Actions cache 跨 workflow 传递。
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_PRED_FILE = os.environ.get("PREDICTIONS_FILE", "/tmp/predictions.json")


def save_predictions(predictions: list[dict], path: str = _PRED_FILE) -> None:
    import datetime
    data = {
        "scan_date": str(datetime.date.today()),
        "predictions": predictions,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("已保存 %d 条预测记录", len(predictions))


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
    """获取该股票上次预测记录，供 AI 参考。"""
    data = load_predictions(path)
    return [p for p in data.get("predictions", []) if p.get("symbol") == symbol]
