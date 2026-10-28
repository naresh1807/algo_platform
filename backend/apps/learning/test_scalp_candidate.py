from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from django.test import SimpleTestCase
from django.utils import timezone
from .scalp_candidate import split_rows, evaluate


class ScalpCandidateTests(SimpleTestCase):
    def rows(self):
        start = datetime(2026, 8, 1, 4, tzinfo=dt_timezone.utc)
        return [SimpleNamespace(opened_at=start + timedelta(days=i), closed_at=start + timedelta(days=i, minutes=5)) for i in range(10)]

    def test_whole_days_do_not_overlap(self):
        train, validation, test = split_rows(self.rows())
        self.assertEqual([len(train), len(validation), len(test)], [6, 2, 2])
        train_days = {timezone.localdate(r.opened_at) for r in train}
        validation_days = {timezone.localdate(r.opened_at) for r in validation}
        test_days = {timezone.localdate(r.opened_at) for r in test}
        self.assertFalse(train_days & validation_days or validation_days & test_days or train_days & test_days)

    def test_overlapping_labels_are_purged(self):
        rows = self.rows()
        rows[5].closed_at = rows[6].opened_at + timedelta(minutes=5)
        rows[7].closed_at = rows[8].opened_at
        train, validation, test = split_rows(rows)
        self.assertNotIn(rows[5], train)
        self.assertNotIn(rows[7], validation)
        self.assertEqual(len(test), 2)

    def test_insufficient_days_rejected(self):
        with self.assertRaises(ValueError):
            split_rows(self.rows()[:4])

    def test_empty_selection_is_reported_without_fake_success(self):
        import numpy as np
        class Model:
            def predict_proba(self, X):
                return np.array([[.9, .1] for _ in X])
        rows = [SimpleNamespace(method="test", symbol="NIFTY", pnl=1, r_multiple=1),
                SimpleNamespace(method="test", symbol="NIFTY", pnl=-1, r_multiple=-1)]
        result = evaluate(Model(), rows, [], .6)
        self.assertEqual(result["coverage"], 0)
        self.assertIsNone(result["selected_trades"]["win_rate"])
        self.assertEqual(result["all_trades"]["mean_r"], 0)
