"""
apps.market_data.management.commands.run_live_feed -- the platform's
own entry point had a real, shipped bug (`NameError: name 'settings'
is not defined`, caught only by actually running it, since
`manage.py check` never executes a command's handle() body and no
other test in this codebase invoked it) after an edit dropped its
`from django.conf import settings` import while removing the old
`_load_option_tokens()` helper. These tests exercise handle() itself
(not just import the module) specifically so a similar regression
fails the test suite instead of only surfacing when someone actually
runs `python manage.py run_live_feed`.
"""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings


class RunLiveFeedCommandTests(SimpleTestCase):
    @override_settings(BROKER_MODE="paper")
    def test_paper_mode_exits_cleanly_without_a_nameerror(self):
        """The exact code path that shipped broken: settings.BROKER_MODE != 'live' -> warn and return."""
        stderr = StringIO()
        call_command("run_live_feed", stderr=stderr)
        self.assertIn("BROKER_MODE='paper'", stderr.getvalue())

    @override_settings(BROKER_MODE="live")
    @patch("apps.market_data.broker_ws_client.LiveFeedClient")
    def test_live_mode_constructs_client_with_the_subscription_manager_provider(self, mock_client_cls):
        """
        BROKER_MODE=live must reach LiveFeedClient construction (not
        raise before it) and wire compute_desired_option_tokens as the
        option_tokens_provider -- run_forever() itself is mocked out
        (it blocks forever for real).
        """
        mock_client = mock_client_cls.return_value
        mock_client.run_forever.side_effect = KeyboardInterrupt

        call_command("run_live_feed")

        self.assertTrue(mock_client_cls.called)
        _, kwargs = mock_client_cls.call_args
        from apps.options.subscription_manager import compute_desired_option_tokens

        self.assertIs(kwargs["option_tokens_provider"], compute_desired_option_tokens)
