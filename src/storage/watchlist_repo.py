from .database import get_conn


def get_all_symbols() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()
    return [r["symbol"] for r in rows]


def add_symbol(symbol: str) -> bool:
    symbol = symbol.upper().strip()
    try:
        with get_conn() as conn:
            conn.execute("INSERT INTO watchlist (symbol) VALUES (?)", (symbol,))
        return True
    except Exception:
        return False


def remove_symbol(symbol: str) -> bool:
    symbol = symbol.upper().strip()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
    return cur.rowcount > 0


def symbol_exists(symbol: str) -> bool:
    symbol = symbol.upper().strip()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM watchlist WHERE symbol = ?", (symbol,)
        ).fetchone()
    return row is not None


def seed_defaults(symbols: list[str]) -> None:
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
        if count == 0:
            for sym in symbols:
                try:
                    conn.execute(
                        "INSERT INTO watchlist (symbol) VALUES (?)", (sym.upper(),)
                    )
                except Exception:
                    pass


def add_subscriber(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,)
        )


def get_all_chat_ids() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT chat_id FROM subscribers").fetchall()
    return [r["chat_id"] for r in rows]
