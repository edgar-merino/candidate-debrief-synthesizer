"""Dedicated structured JSON logging formatter with OTel correlation and PII redaction."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace

from debrief_agent.pii import redact_payload, redact_pii

# Standard LogRecord attributes that should not be duplicated into structured payload.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonLogFormatter(logging.Formatter):
    """Formats Python log records as single-line, PII-redacted Cloud Logging JSON."""

    def __init__(self, *, service_name: str | None = None) -> None:
        super().__init__()
        self.service_name = service_name or os.getenv(
            "OTEL_SERVICE_NAME", "debrief-synthesizer"
        )

    def format(self, record: logging.LogRecord) -> str:
        raw_message = record.getMessage()
        redacted_message, pii_types = redact_pii(raw_message)

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "severity": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": redacted_message,
        }

        # Correlate logs with the active OpenTelemetry span when present.
        span = trace.get_current_span()
        span_ctx = span.get_span_context() if span else None
        if span_ctx and span_ctx.is_valid:
            payload["trace_id"] = format(span_ctx.trace_id, "032x")
            payload["span_id"] = format(span_ctx.span_id, "016x")

        if pii_types:
            payload["pii_redacted_types"] = pii_types

        # Extract structured `extra={...}` fields and redact any PII within them.
        extra_fields = {
            key: redact_payload(val)
            for key, val in record.__dict__.items()
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_")
        }
        if extra_fields:
            payload["attributes"] = extra_fields

        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            payload["exception"], _ = redact_pii(exc_text)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_structured_logging(
    logger_name: str = "debrief_agent",
    level: str | None = None,
) -> logging.Logger:
    """Attaches JsonLogFormatter to the target logger once (idempotent)."""
    target_logger = logging.getLogger(logger_name)
    resolved_level = getattr(
        logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO
    )
    target_logger.setLevel(resolved_level)

    if not any(
        isinstance(h.formatter, JsonLogFormatter) for h in target_logger.handlers
    ):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        target_logger.addHandler(handler)
        target_logger.propagate = False

    return target_logger
