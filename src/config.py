import os
import yaml
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "config", ".env"))


class Config:
    # Moomoo OpenD 连接（无需 API Key）
    MOOMOO_HOST: str = os.environ.get("MOOMOO_HOST", "127.0.0.1")
    MOOMOO_PORT: int = int(os.environ.get("MOOMOO_PORT", "11111"))

    TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    DB_PATH: str = os.environ.get("DB_PATH", "./data/signals.db")
    FINNHUB_API_KEY: str = os.environ.get("FINNHUB_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

    def __init__(self) -> None:
        with open("config/settings.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self.scheduler = cfg["scheduler"]
        self.signals = cfg["signals"]
        self.indicators = cfg["indicators"]
        self.watchlist_defaults: list[str] = cfg["watchlist"]["default_symbols"]
        self.moomoo_group: str = cfg["watchlist"].get("moomoo_group", "US")
        self.pinned_symbols: list[str]       = cfg["watchlist"].get("pinned_symbols", [])
        self.tier_core: list[str]             = cfg["watchlist"].get("tier_core", [])
        self.tier_swing: list[str]            = cfg["watchlist"].get("tier_swing", [])
        self.tier_speculative: list[str]      = cfg["watchlist"].get("tier_speculative", [])
        p = cfg.get("portfolio", {})
        self.total_capital: float = float(p.get("total_capital", 50000))
        self.currency: str = p.get("currency", "AUD")
        self.max_position_pct: float = float(p.get("max_position_pct", 0.10))
        self.log_level: str = cfg["logging"]["level"]
        self.log_file: str = cfg["logging"]["file"]
        self.economic_calendar: dict = cfg.get("economic_calendar", {})
        self.data_source: str = cfg.get("data_source", "yfinance")
