"""
apps.admin_tools.exports.export_closed_trades_csv -- regression coverage
added after discovering this entire file had been silently corrupted to
2213 null bytes in a past commit (git history still had the real,
correct implementation in the very first commit, which is what this
module was restored from). It was never caught because it's only
imported lazily, inside a function, from apps.jarvis.responses' Admin-
only "export_trades" JARVIS command -- nothing exercised the import
path at Django startup or in the existing test suite. This file exists
so a future accidental corruption of this module fails CI immediately
instead of sitting silent again.
"""

from __future__ import annotations

import csv
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.execution.models import OpenPosition
from apps.signals.models import TradingSignal
from common.constants import SignalStatus, SignalType

from .exports import export_closed_trades_csv


class ExportClosedTradesCsvTests(TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._export_dir_patch = patch("apps.admin_tools.exports.EXPORT_DIR", Path(self._tmpdir.name))
        self._export_dir_patch.start()
        self.addCleanup(self._export_dir_patch.stop)

    def _closed_position(self, symbol="NIFTY", pnl="500.00"):
        signal = TradingSignal.objects.create(
            symbol=symbol, signal_type=SignalType.BUY, entry_price=100, stop_loss=95,
            total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
            regime="trending", status=SignalStatus.EXECUTED, reason="test",
        )
        return OpenPosition.objects.create(
            signal=signal, symbol=symbol, side="long", qty=10,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            unrealized_pnl=Decimal(pnl), closed_at=timezone.now(),
        )

    def test_returns_none_and_zero_when_no_closed_trades_exist(self):
        path, count = export_closed_trades_csv()

        self.assertIsNone(path)
        self.assertEqual(count, 0)

    def test_writes_a_real_csv_with_one_row_per_closed_position(self):
        self._closed_position(symbol="NIFTY", pnl="500.00")
        self._closed_position(symbol="BANKNIFTY", pnl="-120.50")
        # An OPEN position (closed_at=None) must never appear in the export.
        OpenPosition.objects.create(
            signal=TradingSignal.objects.create(
                symbol="SENSEX", signal_type=SignalType.BUY, entry_price=100, stop_loss=95,
                total_score=1, technical_score=1, sentiment_score=0, risk_score=1,
                regime="trending", status=SignalStatus.EXECUTED, reason="test",
            ),
            symbol="SENSEX", side="long", qty=5, entry_price=Decimal("100"), stop_loss=Decimal("95"),
        )

        path, count = export_closed_trades_csv()

        self.assertEqual(count, 2)
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        with open(path, newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], [
            "symbol", "side", "qty", "entry_price", "stop_loss", "target_price",
            "exit_pnl", "opened_at", "closed_at",
        ])
        symbols_in_file = {row[0] for row in rows[1:]}
        self.assertEqual(symbols_in_file, {"NIFTY", "BANKNIFTY"})
