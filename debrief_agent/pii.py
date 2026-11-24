"""Active PII redaction pipeline for logging, tool payloads, and memory persistence.

Supports Google Cloud DLP (`deidentify_content`) when `ENABLE_CLOUD_DLP=true` and
`GOOGLE_CLOUD_PROJECT` are configured, with a deterministic local regex redactor
always active as a zero-latency baseline and offline fallback.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger("debrief_agent.pii")

_LOCAL_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "EMAIL_ADDRESS",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    (
        "PHONE_NUMBER",
        re.compile(
            r"(?<!\w)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}(?!\w)"
        ),
    ),
    (
        "US_SOCIAL_SECURITY_NUMBER",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    (
        "CREDIT_CARD_NUMBER",
        re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
    ),
    (
        "SECRET_TOKEN",
        re.compile(r"\b(?:AIza[0-9A-Za-z_-]{20,}|github_pat_[0-9A-Za-z_]{20,}|sk-[0-9A-Za-z]{20,})\b"),
    ),
)

_DEFAULT_DLP_INFO_TYPES: tuple[str, ...] = (
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    "CREDIT_CARD_NUMBER",
    "PERSON_NAME",
)


class PiiRedactor:
    """Redacts sensitive PII using Cloud DLP (when enabled) + local regex rules."""

    def __init__(
        self,
        *,
        use_cloud_dlp: bool | None = None,
        project_id: str | None = None,
        info_types: tuple[str, ...] = _DEFAULT_DLP_INFO_TYPES,
    ) -> None:
        self.project_id = project_id or os.getenv("GOOGLE_CLOUD_PROJECT")
        if use_cloud_dlp is None:
            self.use_cloud_dlp = (
                os.getenv("ENABLE_CLOUD_DLP", "").lower() in ("true", "1", "yes")
                and bool(self.project_id)
            )
        else:
            self.use_cloud_dlp = use_cloud_dlp and bool(self.project_id)
        self.info_types = info_types
        self._dlp_client: Any = None

    def _get_dlp_client(self) -> Any:
        if self._dlp_client is None:
            from google.cloud import dlp_v2  # type: ignore[import-untyped]

            self._dlp_client = dlp_v2.DlpServiceClient()
        return self._dlp_client

    def _redact_with_cloud_dlp(self, text: str) -> tuple[str, list[str]]:
        """Calls Cloud DLP deidentify_content to mask detected info types."""
        client = self._get_dlp_client()
        parent = f"projects/{self.project_id}/locations/global"
        inspect_config = {
            "info_types": [{"name": info_type} for info_type in self.info_types]
        }
        deidentify_config = {
            "info_type_transformations": {
                "transformations": [
                    {
                        "primitive_transformation": {
                            "replace_with_info_type_config": {}
                        }
                    }
                ]
            }
        }
        response = client.deidentify_content(
            request={
                "parent": parent,
                "inspect_config": inspect_config,
                "deidentify_config": deidentify_config,
                "item": {"value": text},
            }
        )
        redacted_text = response.item.value
        findings = [
            info_type
            for info_type in self.info_types
            if f"[{info_type}]" in redacted_text
        ]
        return redacted_text, findings

    def redact_text(self, text: str) -> tuple[str, list[str]]:
        """Redacts PII from text and returns (redacted_text, detected_types)."""
        if not text:
            return text, []

        detected: list[str] = []
        current = text

        if self.use_cloud_dlp:
            try:
                current, dlp_findings = self._redact_with_cloud_dlp(current)
                detected.extend(dlp_findings)
            except Exception as exc:
                logger.debug(
                    "Cloud DLP unavailable; falling back to local PII redactor: %s",
                    exc,
                )

        for label, pattern in _LOCAL_PII_PATTERNS:
            replaced, count = pattern.subn(f"[REDACTED_{label}]", current)
            if count > 0:
                current = replaced
                if label not in detected:
                    detected.append(label)

        return current, detected

    def redact_value(self, value: Any) -> Any:
        """Recursively redacts PII inside strings, dicts, and lists."""
        if isinstance(value, str):
            redacted, _ = self.redact_text(value)
            return redacted
        if isinstance(value, dict):
            return {str(k): self.redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        return value


_default_redactor = PiiRedactor()


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Redacts PII from text using the process-wide PiiRedactor."""
    return _default_redactor.redact_text(text)


def redact_payload(payload: Any) -> Any:
    """Recursively redacts PII from structured log or tool payloads."""
    return _default_redactor.redact_value(payload)
