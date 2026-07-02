import sqlite3
import os
from contextlib import contextmanager


_db_path: str = ""


def init_db(db_path: str) -> None:
    global _db_path
    _db_path = db_path
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol   TEXT NOT NULL UNIQUE,
                added_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS signals (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol           TEXT NOT NULL,
                generated_at     TEXT NOT NULL DEFAULT (datetime('now')),
                direction        TEXT NOT NULL CHECK(direction IN ('BUY','SELL','NEUTRAL')),
                strength         INTEGER NOT NULL,
                close_price      REAL NOT NULL,
                rsi              REAL,
                reasons          TEXT NOT NULL,
                notified         INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_signals_symbol
                ON signals(symbol, generated_at);

            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id       INTEGER PRIMARY KEY,
                subscribed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)


@contextmanager
def get_conn():
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
