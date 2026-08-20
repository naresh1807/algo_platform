"""
common.log_filters.RedactSensitiveDataFilter -- fix-list item 6/11's
"never log Authorization headers, JWT tokens, API keys, passwords,
TOTP secrets" requirement. Pure logging.Filter unit tests, no Django DB.
"""

import logging
import unittest

from common.log_filters import RedactSensitiveDataFilter


def _filtered_message(msg, *args):
    record = logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, None)
    RedactSensitiveDataFilter().filter(record)
    return record.getMessage()


class RedactSensitiveDataFilterTests(unittest.TestCase):
    def test_authorization_header_is_redacted(self):
        out = _filtered_message("Request headers: Authorization: Bearer abc123.def456.ghi789")
        self.assertNotIn("abc123.def456.ghi789", out)
        self.assertIn("***REDACTED***", out)

    def test_jwt_token_field_is_redacted(self):
        out = _filtered_message("session data: jwtToken=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", out)

    def test_feed_token_field_is_redacted(self):
        out = _filtered_message("creds: feed_token=abcdef0123456789")
        self.assertNotIn("abcdef0123456789", out)

    def test_api_key_is_redacted(self):
        out = _filtered_message("api_key=sk_live_1234567890abcdef sent to broker")
        self.assertNotIn("sk_live_1234567890abcdef", out)

    def test_password_is_redacted(self):
        out = _filtered_message("login failed for password=SuperSecret123!")
        self.assertNotIn("SuperSecret123!", out)

    def test_totp_secret_is_redacted(self):
        out = _filtered_message("totp_secret=JBSWY3DPEHPK3PXP generated code")
        self.assertNotIn("JBSWY3DPEHPK3PXP", out)

    def test_x_api_key_header_is_redacted(self):
        out = _filtered_message("headers: x-api-key=abcd1234efgh5678")
        self.assertNotIn("abcd1234efgh5678", out)

    def test_message_with_no_secret_is_unchanged(self):
        original = "Angel One live feed connected -- subscribing to 8 symbols."
        out = _filtered_message(original)
        self.assertEqual(out, original)

    def test_filter_never_raises_on_non_string_args(self):
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "value=%s", (12345,), None)
        # Must not raise even though the message has %-style args.
        self.assertTrue(RedactSensitiveDataFilter().filter(record))

    def test_filter_return_value_always_true(self):
        """A logging.Filter returning False would SUPPRESS the record entirely -- this filter only redacts, never drops a log line."""
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "password=hunter2", (), None)
        self.assertTrue(RedactSensitiveDataFilter().filter(record))
