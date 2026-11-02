"""structlog setup shared by the API and the worker.

All logs (ours, uvicorn's, Celery's) go through the stdlib root logger and are rendered by
structlog, so every line carries the same context (request_id, job_id, ...) bound via
structlog.contextvars.
"""

import logging
from typing import Literal

import structlog
from structlog.typing import Processor

LogFormat = Literal["console", "json"]
_HANDLER_NAME = "docs_extraction"


def configure_logging(level: str = "INFO", fmt: LogFormat = "console") -> None:
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: list[Processor] = (
        [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
        if fmt == "json"
        else [structlog.dev.ConsoleRenderer()]
    )
    handler = logging.StreamHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderer],
        )
    )

    # Replace only our own handler, so repeated calls are idempotent and handlers added by
    # others (e.g. pytest's log capture) are kept.
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != _HANDLER_NAME] + [handler]
    root.setLevel(level)

    # Route uvicorn through the root handler; its access log is replaced by our request log.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # Third-party HTTP clients are chatty at INFO.
    for name in ("httpx", "httpx2", "openai", "anthropic", "urllib3"):
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
