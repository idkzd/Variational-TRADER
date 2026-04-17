"""
Logging setup for the Variational Omni Trading Bot.

Provides:
  - Console output (coloured via standard logging)
  - Rotating file output (keeps last 5 × 5 MB log files)
  - A trade-specific logger that writes CSV-style records for post-analysis
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import LoggingConfig


def setup_logging(cfg: LoggingConfig) -> logging.Logger:
    """
    Initialise and return the root bot logger.

    Args:
        cfg: LoggingConfig dataclass with level, file path, format, etc.

    Returns:
        The configured root logger named 'variational_bot'.
    """

    logger = logging.getLogger("variational_bot")
    logger.setLevel(getattr(logging, cfg.level.upper(), logging.INFO))

    # Prevent duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    formatter = logging.Formatter(fmt=cfg.fmt, datefmt=cfg.datefmt)

    # ── Console handler ──
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ── Rotating file handler (5 MB × 5 backups) ──
    log_dir = Path(cfg.log_file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        cfg.log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_trade_logger(log_dir: str = "trade_logs") -> logging.Logger:
    """
    Return a dedicated logger that writes one CSV line per completed trade.

    This makes it easy to analyse trades in a spreadsheet after the fact.

    Columns:
        timestamp, trade_id, symbol, side, entry, exit, size, leverage, pnl, outcome, duration
    """

    Path(log_dir).mkdir(parents=True, exist_ok=True)

    trade_logger = logging.getLogger("trade_records")
    trade_logger.setLevel(logging.INFO)

    if trade_logger.handlers:
        return trade_logger

    csv_handler = RotatingFileHandler(
        f"{log_dir}/trades.csv",
        maxBytes=2 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    csv_handler.setFormatter(logging.Formatter("%(message)s"))
    trade_logger.addHandler(csv_handler)

    # Write header if file is empty / new
    csv_path = Path(f"{log_dir}/trades.csv")
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        trade_logger.info(
            "timestamp,trade_id,symbol,side,entry_price,exit_price,"
            "size_usdc,leverage,pnl_usdc,outcome,duration_s"
        )

    return trade_logger
