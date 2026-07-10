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
    # .strip() 很重要：GitHub Secrets 粘贴时容易带上尾随换行，混进HTTP header
    # 会被 httpcore 判定为非法header值直接拒绝发送（而不是被API拒绝），
    # 报错信息是"Connection error"，很难联想到是密钥本身多了个\n
    FINNHUB_API_KEY: str = os.environ.get("FINNHUB_API_KEY", "").strip()
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    # 影子模式用：免费模型，只记录不下单，不配置则自动跳过整个影子流程
    GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "").strip()

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
        self.alpaca_capital_base: float = float(cfg.get("alpaca", {}).get("capital_base_usd", 50000))
        self.log_level: str = cfg["logging"]["level"]
        self.log_file: str = cfg["logging"]["file"]
        self.economic_calendar: dict = cfg.get("economic_calendar", {})
        self.data_source: str = cfg.get("data_source", "yfinance")

        try:
            with open("config/universe.yaml", encoding="utf-8") as f:
                uni = yaml.safe_load(f) or {}
        except FileNotFoundError:
            uni = {}
        self.universe_sp500: list[str]     = uni.get("sp500", [])
        self.universe_china_adr: list[str] = uni.get("china_adr", [])
