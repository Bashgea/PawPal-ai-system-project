"""logging_setup.py — central logging configuration for PawPal+.

Call configure() once at application startup. Safe to call multiple times;
subsequent calls are no-ops if the root logger already has handlers.
"""

import logging

import config


def configure() -> None:
    """Configure the root logger from config.LOG_LEVEL.

    Uses basicConfig which is idempotent — calling it when handlers are already
    attached is a no-op, so this is safe to call on every Streamlit re-run.
    """
    logging.basicConfig(
        level   = getattr(logging, config.LOG_LEVEL, logging.INFO),
        format  = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt = "%H:%M:%S",
    )
