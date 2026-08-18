from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from . import engine
from .commands import COMMANDS, FORBIDDEN_ACTIONS, RESTRICTED_ACTIONS
from .intent import detect_intent
from .models import JarvisCommandHistory, JarvisMemory


class IntentDetectionTests(TestCase):
    def test_exact_phrase_matches_with_full_confidence(self):
        intent = detect_intent("show risk")
        self.assertEqual(intent.action, "show_risk")
        self.assertEqual(intent.confidence, 1.0)

    def test_case_and_whitespace_insensitive(self):
        intent = detect_intent("   SHOW   Risk  ")
        self.assertEqual(intent.action, "show_risk")

    def test_unrecognized_text_returns_no_action(self):
        intent = detect_intent("what's the meaning of life")
        self.assertIsNone(intent.action)

    def test_longer_phrase_wins_over_shorter_generic_one(self):
        # "show risk" must not be shadowed by a hypothetical generic "show"
        # entry -- exercises the length-first ranking in intent.py.
        intent = detect_intent("please show risk report now")
        self.assertIn(intent.action, {"risk_report", "show_risk"})

    def test_every_registered_command_has_a_handler(self):
        from . import responses
        missing = [a for a in COMMANDS if not hasattr(responses, a)]
        self.assertEqual(missing, [], f"Commands with no responses.py handler: {missing}")


class CommandEngineSecurityTests(TestCase):
    """manual 14.21: Security Rules -- this is what actually enforces them."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="pw")

    def test_forbidden_action_never_executes(self):
        self.assertTrue(len(FORBIDDEN_ACTIONS) > 0)
        # No forbidden action is even reachable via intent detection
        # today (none has phrases registered in commands.py) -- this
        # asserts that invariant so a future edit can't accidentally
        # wire one up without this test failing.
        for action in FORBIDDEN_ACTIONS:
            self.assertNotIn(action, COMMANDS, f"{action} must never be a dispatchable command")

    def test_restricted_action_requires_confirmation(self):
        response = engine.process("reset portfolio", self.user, confirm=False)
        self.assertTrue(response.needs_confirmation)
        self.assertEqual(response.action, "reset_portfolio")

    def test_restricted_action_with_confirm_does_not_silently_reset_anything(self):
        response = engine.process("reset portfolio", self.user, confirm=True)
        self.assertFalse(response.needs_confirmation)
        self.assertIn("deliberately stops short", response.text)

    def test_every_restricted_action_is_gated(self):
        for action in RESTRICTED_ACTIONS:
            self.assertIn(action, COMMANDS, f"{action} should be a real command, just confirmation-gated")


class CommandEngineHistoryTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="pw")

    def test_every_command_writes_a_history_row(self):
        before = JarvisCommandHistory.objects.count()
        engine.process("kill switch status", self.user)
        self.assertEqual(JarvisCommandHistory.objects.count(), before + 1)

    def test_unrecognized_command_still_logs_and_answers(self):
        # local_llm.is_configured() makes a real network call to Ollama
        # (see that function's own docstring) -- on a machine that
        # happens to have Ollama actually running (true on this dev
        # box), an unmocked call here would go down the real free-form-
        # LLM path and get a real answer back (success=True), instead of
        # testing what this test is actually about: the canned
        # "I didn't recognize that command" fallback still logs and
        # answers. Mocked False so the outcome doesn't depend on
        # whatever service happens to be running on the machine the
        # suite executes on.
        from unittest.mock import patch

        with patch("apps.jarvis.local_llm.is_configured", return_value=False):
            response = engine.process("do a backflip", self.user)
        self.assertFalse(response.success)
        self.assertTrue(response.text)

    def test_memory_updated_after_recognized_command(self):
        engine.process("show risk", self.user)
        memory = JarvisMemory.objects.get(user=self.user)
        self.assertEqual(memory.context_json["last_intent"], "show_risk")


class JarvisCommandAPITests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="a-real-password-123")

    def test_command_endpoint_requires_auth(self):
        response = self.client.post("/api/jarvis/command/", {"text": "show risk"})
        self.assertEqual(response.status_code, 401)

    def test_command_endpoint_returns_structured_response(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/jarvis/command/", {"text": "open portfolio"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["action"], "open_portfolio")
        self.assertEqual(response.data["route"], "/positions")

    def test_suggested_commands_endpoint(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/jarvis/suggested/", {"category": "risk"})
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data["commands"]), 0)


class AnnouncementCoverageTests(TestCase):
    """
    manual 14.16 "JARVIS Announces" lists 8 event kinds. This doesn't
    exercise the actual channel_layer broadcast (that needs a running
    Channels layer) -- it just asserts the wiring exists: a receiver
    or scheduled task for each kind, so a future refactor that quietly
    drops one fails a test instead of only being noticed the next time
    that event happens to occur.
    """

    def test_all_manual_1416_announcement_kinds_are_wired(self):
        import inspect

        from . import signals as jarvis_signals
        from . import tasks as jarvis_tasks
        from apps.admin_tools.management.commands import backup_database as backup_cmd

        signals_src = inspect.getsource(jarvis_signals)
        tasks_src = inspect.getsource(jarvis_tasks)
        backup_src = inspect.getsource(backup_cmd)
        combined = signals_src + tasks_src + backup_src

        expected_kinds = [
            "market_open", "market_close", "trade_executed", "signal_generated",
            "risk_warning", "kill_switch", "ai_training_completed", "database_backup_completed",
        ]
        missing = [k for k in expected_kinds if f'"{k}"' not in combined]
        self.assertEqual(missing, [], f"manual 14.16 announcement kinds with no wiring found: {missing}")


class PriceAlertCommandTests(APITestCase):
    """create_price_alert / list_price_alerts -- trader-facing feature, not manual-specified."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="a-real-password-123")

    def test_create_alert_with_explicit_direction(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/jarvis/command/", {"text": "alert me when NIFTY above 25000"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["action"], "create_price_alert")
        self.assertIn("25000", response.data["text"])

        from apps.monitoring.models import PriceAlert
        alert = PriceAlert.objects.get(created_by=self.user)
        self.assertEqual(alert.symbol, "NIFTY")
        self.assertEqual(alert.condition, "above")
        self.assertEqual(float(alert.target_price), 25000.0)

    def test_create_alert_requires_symbol(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/jarvis/command/", {"text": "set alert 25000"})
        self.assertIn("Which symbol", response.data["text"])

    def test_list_alerts_when_none_exist(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/jarvis/command/", {"text": "show alerts"})
        self.assertIn("No active price alerts", response.data["text"])


class StockWatchlistCommandTests(APITestCase):
    """apps.investing -- trader-requested, not manual-specified."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="a-real-password-123")

    def test_add_to_stock_watchlist_extracts_symbol_from_utterance(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/jarvis/command/", {"text": "add TCS to my stock watchlist"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["action"], "add_to_stock_watchlist")
        self.assertIn("TCS", response.data["text"])

        from apps.investing.models import StockWatchlist
        entry = StockWatchlist.objects.get(user=self.user)
        self.assertEqual(entry.stock.symbol, "TCS")

    def test_empty_watchlist_message(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/jarvis/command/", {"text": "my stocks"})
        self.assertIn("empty", response.data["text"])


class MarketIntelligenceTests(TestCase):
    """
    apps.jarvis.market_intelligence -- the AI/ML synthesis layer, not
    manual-specified. These deliberately only cover the "no data yet"
    paths (no real signals/news/options/recommendations exist in a
    fresh test DB) -- the synthesis logic itself just reads other
    apps' own models, which those apps' own test suites already cover.
    """

    def test_market_outlook_with_no_data_still_returns_a_full_report(self):
        from .market_intelligence import market_outlook

        result = market_outlook()
        self.assertIn(result["bias"], ("Bullish", "Bearish", "Neutral"))
        self.assertIsInstance(result["summary"], str)
        self.assertIn("not investment or trading advice", result["summary"].lower())

    def test_no_options_idea_when_bias_is_neutral(self):
        from .market_intelligence import market_outlook

        result = market_outlook()
        if result["bias"] == "Neutral":
            self.assertIsNone(result["options_idea"])

    def test_no_stock_idea_when_no_recommendations_exist(self):
        from .market_intelligence import market_outlook

        result = market_outlook()
        self.assertIsNone(result["stock_idea"])

    def test_index_bias_is_bearish_for_an_approved_sell_signal(self):
        """
        apps.options.index_direction_strategy's PE-side case produces
        APPROVED SELL signals -- _index_bias must read those as
        "Bearish", not fall through to the generic "Neutral" default
        every non-BUY status previously did.
        """
        from django.test import override_settings

        from apps.signals.models import TradingSignal
        from common.constants import SignalStatus, SignalType

        from .market_intelligence import _index_bias

        with override_settings(WATCHLIST=["NIFTY"]):
            TradingSignal.objects.create(
                symbol="NIFTY", signal_type=SignalType.SELL, entry_price=100, stop_loss=105,
                total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
                regime="trending", status=SignalStatus.APPROVED, reason="test",
            )
            result = _index_bias()
        self.assertEqual(result["per_symbol"][0]["bias"], "Bearish")


class GenerateMarketOutlookTaskTests(TestCase):
    def test_first_run_creates_a_snapshot_and_does_not_announce(self):
        from .models import MarketOutlookSnapshot
        from .tasks import generate_market_outlook

        result = generate_market_outlook()
        self.assertFalse(result["changed"])  # no previous snapshot to compare against
        self.assertEqual(MarketOutlookSnapshot.objects.count(), 1)

    def test_bias_change_between_runs_is_detected(self):
        from .models import MarketOutlookSnapshot
        from .tasks import generate_market_outlook

        MarketOutlookSnapshot.objects.create(bias="Bullish", summary="prior run")
        result = generate_market_outlook()
        # Fresh test DB has no signals -> new run will read as Neutral, differing from the seeded "Bullish".
        self.assertTrue(result["changed"])


class MarketOutlookCommandTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="trader1", password="a-real-password-123")

    def test_market_outlook_command_computes_on_the_fly_with_no_snapshots(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/jarvis/command/", {"text": "market outlook"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["action"], "market_outlook")
        self.assertIn("Market bias", response.data["text"])
