"""
美股交易信号系统 — 主入口。
启动顺序: 数据库初始化 → 数据客户端 → Telegram Bot (含调度器) → 开始轮询

数据源由 DATA_SOURCE 环境变量或 settings.yaml 控制：
  yfinance  — Yahoo Finance，无需本地依赖，支持云端部署（默认）
  moomoo    — Moomoo OpenD，需本地运行 OpenD
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from src.config import Config
from src.storage.database import init_db
from src.storage import watchlist_repo, signal_repo
from src.notifications.bot import build_application


def setup_logging(config: Config) -> None:
    os.makedirs(os.path.dirname(config.log_file), exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.log_file, encoding="utf-8"),
        ],
    )


def _build_data_client(config: Config):
    source = os.environ.get("DATA_SOURCE", config.data_source).lower()
    if source == "moomoo":
        from src.data.moomoo_client import MoomooDataClient
        logger = logging.getLogger(__name__)
        logger.info("数据源: Moomoo OpenD (%s:%d)", config.MOOMOO_HOST, config.MOOMOO_PORT)
        return MoomooDataClient(host=config.MOOMOO_HOST, port=config.MOOMOO_PORT)
    else:
        from src.data.yfinance_client import YFinanceDataClient
        logger = logging.getLogger(__name__)
        logger.info("数据源: Yahoo Finance (yfinance)")
        return YFinanceDataClient()


def main() -> None:
    config = Config()
    setup_logging(config)
    logger = logging.getLogger(__name__)
    logger.info("系统启动...")

    # 初始化数据库和 Excel
    init_db(config.DB_PATH)
    excel_path = config.DB_PATH.replace(".db", ".xlsx")
    signal_repo.init_excel(excel_path)
    logger.info("数据库已初始化: %s", config.DB_PATH)

    # 播种自选股：优先用完整列表（所有 tier），fallback 用 default_symbols
    all_symbols = list(dict.fromkeys(
        config.tier_core + config.tier_swing + config.tier_speculative
        + config.pinned_symbols
    ))
    watchlist_repo.seed_defaults(all_symbols or config.watchlist_defaults)

    data_client = _build_data_client(config)

    # 构建 Telegram Application（内部通过 post_init 启动调度器）
    app = build_application(config)
    app.bot_data["data_client"] = data_client
    app.bot_data["config"] = config

    logger.info("Telegram Bot 开始轮询，按 Ctrl+C 停止")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
