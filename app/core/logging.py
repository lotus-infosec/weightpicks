"""structlog setup: JSON lines in production, a readable console renderer in dev."""

import logging
from typing import Literal

import structlog


def configure_logging(level: str = "INFO", fmt: Literal["json", "console"] = "json") -> None:
    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer() if fmt == "console" else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
