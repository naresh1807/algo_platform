"""
Log-record redaction -- config.settings.LOGGING attaches this filter to
every handler so a log line that happens to include an HTTP header dict,
a raw broker session response, or an exception message containing a
secret can never write that secret to disk/console verbatim.

Deliberately pattern-based, applied to the fully-formatted message
(getMessage()), not a whitelist of "safe" call sites -- this codebase
logs broker responses/exceptions in many places (apps.market_data.
broker_client, apps.options.broker_client, apps.market_data.
broker_ws_client, ...), and a whitelist approach would need to be kept
in sync with every one of them forever. Scanning the rendered text
instead means a NEW log call anywhere in the codebase is covered
automatically, with no additional wiring.
"""

from __future__ import annotations

import logging
import re

_MASK = "***REDACTED***"

# Each pattern captures the sensitive VALUE in group 1 (or is replaced
# whole for header-style "Key: value" lines) so the key/label stays
# readable in the log for debugging context, only the secret itself is
# masked.
_PATTERNS = [
    # Authorization: Bearer <token> / Authorization: <token>
    re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(bearer\s+)?([A-Za-z0-9\-_.=]{8,})"),
    # jwtToken / jwt_token / feedToken / feed_token / api_key / apikey / access_token style fields
    re.compile(r"(?i)((?:jwt|feed|api|access|refresh)[_-]?token['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9\-_.=]{8,})"),
    re.compile(r"(?i)(api[_-]?key['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9\-_.=]{6,})"),
    # password / passwd / totp / totp_secret fields
    re.compile(r"(?i)((?:password|passwd|totp[_-]?secret|totp)['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]{3,})"),
    # x-api-key / x-feed-token / x-client-code style headers (SmartWebSocketV2.connect())
    re.compile(r"(?i)(x-(?:api-key|feed-token|client-code)['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]{3,})"),
]


class RedactSensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True  # never let a broken record formatting crash logging itself

        redacted = message
        for pattern in _PATTERNS:
            redacted = pattern.sub(lambda m: f"{m.group(1)}{_MASK}", redacted)

        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True
