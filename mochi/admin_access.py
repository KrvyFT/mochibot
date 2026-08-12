"""Credential-safe admin URLs and persistent text."""

import logging
import os
import re
import sys
from urllib.parse import urlsplit, urlunsplit


_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_TOKEN_PARAM_RE = re.compile(r"[?&]token=[^&#\s<>'\"]*", re.IGNORECASE)
_ADMIN_TOKEN_ASSIGNMENT_RE = re.compile(
    r"(ADMIN_TOKEN\s*[:=]\s*)[^\s,;]+",
    re.IGNORECASE,
)


def build_admin_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def safe_display_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _known_admin_tokens(explicit_token: str | None = None) -> set[str]:
    tokens = {
        token
        for token in (explicit_token, os.environ.get("ADMIN_TOKEN"))
        if token
    }
    config_module = sys.modules.get("mochi.config")
    if config_module is not None:
        configured = getattr(config_module, "ADMIN_TOKEN", "")
        if configured:
            tokens.add(configured)
    return tokens


def sanitize_persistent_text(
    value: object,
    *,
    token: str | None = None,
) -> str:
    text = str(value)
    text = _URL_RE.sub(lambda match: safe_display_url(match.group(0)), text)
    text = _TOKEN_PARAM_RE.sub("", text)
    text = _ADMIN_TOKEN_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    for known in sorted(_known_admin_tokens(token), key=len, reverse=True):
        text = text.replace(known, "[REDACTED]")
    return text


class SensitiveDataFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return sanitize_persistent_text(super().format(record))


def configure_safe_logging(
    *,
    level: int,
    format_string: str,
    date_format: str,
) -> None:
    logging.basicConfig(
        level=level,
        format=format_string,
        datefmt=date_format,
    )
    formatter = SensitiveDataFormatter(format_string, datefmt=date_format)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
