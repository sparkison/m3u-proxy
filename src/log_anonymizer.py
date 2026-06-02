"""
Log anonymization filter — scrubs URLs and usernames from log records
before they are written. Activated when LOG_ANONYMIZE=true (default).
"""

import logging
import re

_URL_RE = re.compile(
    r'(?:https?|rtmps?|ftps?|hls)://[^\s"\'<>\[\]{}\|\\^`]+',
    re.IGNORECASE,
)
# Matches key=value or key: value, stops before & ) space quote etc.
_USER_RE = re.compile(
    r'(?i)\b(username|user|login|ip)\s*[:=]\s*([^&\s"\'<>#,\)\]]+)',
)
# UUIDs are resource identifiers that can be used to probe APIs
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def _scrub(text: str) -> str:
    text = _URL_RE.sub("****", text)
    text = _USER_RE.sub(r"\1=****", text)
    text = _UUID_RE.sub("****", text)
    return text


class AnonymizingFilter(logging.Filter):
    """Logging filter that redacts URLs and credentials from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn access log records: AccessFormatter reads record.args directly as
        # (client_addr, method, full_path, http_version, status_code) — do not call
        # getMessage() or clear args, as that would break the formatter. Scrub only
        # the full_path component (index 2) which contains the query string.
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) >= 3
        ):
            lst = list(record.args)
            lst[2] = _scrub(str(lst[2]))
            record.args = tuple(lst)
            return True

        # Standard records: getMessage() merges msg + args into the final string.
        # Store the scrubbed result back and clear args to avoid double-formatting.
        record.msg = _scrub(record.getMessage())
        record.args = None
        return True
