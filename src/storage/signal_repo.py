import json
import os
from datetime import datetime
from .database import get_conn

_EXCEL_PATH = ""
_EXCEL_HEADERS = ["时间", "股票", "方向", "强度", "收盘价", "RSI", "信号原因"]


def init_excel(excel_path: str) -> None:
    global _EXCEL_PATH
    _EXCEL_PATH = excel_path
    os.makedirs(os.path.dirname(excel_path), exist_ok=True)
    if not os.path.exists(excel_path):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "信号记录"
        ws.append(_EXCEL_HEADERS)
        # 冻结首行、设置列宽
        ws.freeze_panes = "A2"
        for col, width in zip("ABCDEFG", [20, 8, 6, 6, 10, 8, 60]):
            ws.column_dimensions[col].width = width
        wb.save(excel_path)


def _append_to_excel(symbol: str, direction: str, strength: int,
                     close_price: float, rsi: float, reasons: list[str]) -> None:
    if not _EXCEL_PATH:
        return
    import openpyxl
    wb = openpyxl.load_workbook(_EXCEL_PATH)
    ws = wb.active
    ws.append([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        symbol,
        direction,
        strength,
        round(close_price, 2) if close_price == close_price else None,
        round(rsi, 2) if rsi == rsi else None,
        "；".join(reasons),
    ])
    wb.save(_EXCEL_PATH)


def save_signal(symbol: str, direction: str, strength: int,
                close_price: float, rsi: float, reasons: list[str]) -> int:
    _append_to_excel(symbol, direction, strength, close_price, rsi, reasons)
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO signals
               (symbol, direction, strength, close_price, rsi, reasons)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol, direction, strength, close_price, rsi, json.dumps(reasons, ensure_ascii=False)),
        )
    return cur.lastrowid


def mark_notified(signal_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE signals SET notified = 1 WHERE id = ?", (signal_id,))


def get_unnotified_signals() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE notified = 0 ORDER BY generated_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_signals(symbol: str, days: int = 30) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM signals
               WHERE symbol = ?
                 AND generated_at >= datetime('now', ?)
               ORDER BY generated_at DESC""",
            (symbol, f"-{days} days"),
        ).fetchall()
    return [dict(r) for r in rows]
