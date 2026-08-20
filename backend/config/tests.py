import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse


class ApiUrlNamespaceTests(SimpleTestCase):
    def test_concrete_paths_are_unchanged_by_namespacing(self):
        expected_paths = {
            "auth:api-token-auth": "/api/auth/token/",
            "market_data:indicator-series": "/api/market-data/indicators/",
            "news:market-summary": "/api/news/market-summary/",
            "options:options-chain": "/api/options/chain/",
            "risk:kill-switch-status": "/api/risk/kill-switch/",
            "execution:execution-mode": "/api/execution/mode/",
            "learning:technical-direction": "/api/learning/technical-direction/",
            "analytics:sharpe-ratio": "/api/analytics/sharpe-ratio/",
            "jarvis:jarvis-command": "/api/jarvis/command/",
            "investing:sector-breakdown": "/api/investing/sector-breakdown/",
        }

        for view_name, expected_path in expected_paths.items():
            with self.subTest(view_name=view_name):
                self.assertEqual(reverse(view_name), expected_path)

    def test_each_router_api_root_has_a_unique_namespace(self):
        expected_roots = {
            "market_data:api-root": "/api/market-data/",
            "news:api-root": "/api/news/",
            "options:api-root": "/api/options/",
            "signals:api-root": "/api/signals/",
            "risk:api-root": "/api/risk/",
            "execution:api-root": "/api/execution/",
            "learning:api-root": "/api/learning/",
            "monitoring:api-root": "/api/monitoring/",
            "analytics:api-root": "/api/analytics/",
            "admin_tools:api-root": "/api/admin-tools/",
            "investing:api-root": "/api/investing/",
        }

        for view_name, expected_path in expected_roots.items():
            with self.subTest(view_name=view_name):
                self.assertEqual(reverse(view_name), expected_path)


class CeleryRoutingTests(SimpleTestCase):
    """
    config/celery.py's task_routes -- fix-list item 7 ("make the
    configuration and documentation consistent" for the priority queue).
    A routing-table assertion, not a live Celery/Redis integration test
    (no broker involved) -- verifies the exact tasks that must never be
    starved by slow default-queue work are actually routed to "priority".
    """

    def test_option_chain_ingestion_routed_to_priority_queue(self):
        from config.celery import app

        route = app.conf.task_routes["apps.options.tasks.ingest_option_chain_snapshots"]
        self.assertEqual(route["queue"], "priority")

    def test_priority_worker_heartbeat_routed_to_priority_queue(self):
        from config.celery import app

        route = app.conf.task_routes["apps.monitoring.tasks.heartbeat_priority_worker"]
        self.assertEqual(route["queue"], "priority")

    def test_default_worker_heartbeat_is_not_routed_to_priority(self):
        from config.celery import app

        self.assertNotIn("apps.monitoring.tasks.heartbeat_default_worker", app.conf.task_routes)

    def test_both_heartbeats_are_scheduled(self):
        from config.celery import app

        self.assertIn("heartbeat-default-worker-every-minute", app.conf.beat_schedule)
        self.assertIn("heartbeat-priority-worker-every-minute", app.conf.beat_schedule)


class SecuritySettingsTests(SimpleTestCase):
    def _settings_import(self, command="import config.settings", **overrides):
        child_environment = os.environ.copy()
        child_environment.update(overrides)
        return subprocess.run(
            [sys.executable, "-c", command],
            cwd=settings.BASE_DIR,
            env=child_environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_unknown_environment_is_rejected(self):
        result = self._settings_import(DJANGO_ENVIRONMENT="unknown-environment")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_ENVIRONMENT must be one of", result.stderr)

    def test_short_production_secret_is_rejected(self):
        result = self._settings_import(
            DJANGO_ENVIRONMENT="production",
            DJANGO_DEBUG="0",
            DJANGO_ALLOWED_HOSTS="example.test",
            DJANGO_SECRET_KEY="too-short",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("strong secret", result.stderr)

    def test_proxy_count_must_be_non_negative(self):
        result = self._settings_import(
            DJANGO_ENVIRONMENT="development",
            DRF_NUM_PROXIES="-1",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DRF_NUM_PROXIES", result.stderr)

    def test_normal_runtime_uses_shared_cache_and_explicit_proxy_count(self):
        result = self._settings_import(
            "import config.settings as s; "
            "assert s.CACHES['default']['BACKEND'].endswith('RedisCache'); "
            "assert s.REST_FRAMEWORK['NUM_PROXIES'] == 2",
            DJANGO_ENVIRONMENT="development",
            DJANGO_TESTING="0",
            DRF_NUM_PROXIES="2",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
