"""
Structured logging configuration.

Emits JSON-friendly, single-line log records so the service is easy to
ingest into centralized logging (ELK, Datadog, CloudWatch) when running in
containers/Kubernetes.
"""
import logging
import sys

from src.config import get_settings


class _ContextFormatter(logging.Formatter):
    """Formatter that keeps log lines structured and greppable."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f'time="{self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z")}" '
            f'level={record.levelname} '
            f'logger={record.name} '
            f'msg="{record.getMessage()}"'
        )
        if record.exc_info:
            base += f" exc_info={self.formatException(record.exc_info)!r}"
        return base


def configure_logging() -> None:
    """Configure root logging handlers exactly once for the process."""
    settings = get_settings()
    root = logging.getLogger()

    if root.handlers:
        # Already configured (e.g. re-imported under a test runner).
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_ContextFormatter())

    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Quiet down noisy third-party loggers by default.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
