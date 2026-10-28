from django.test import SimpleTestCase
from .scalp_market_training import label_path


class MarketLabelTests(SimpleTestCase):
    def test_both_touched_counts_stop_first(self):
        net_r, index, reason = label_path(100, 2, [{"open":100,"high":103,"low":97,"close":101}], 1)
        self.assertAlmostEqual(net_r,-1.1)
        self.assertEqual(reason,"stop")

    def test_short_target_and_cost(self):
        net_r, _, reason = label_path(100,2,[{"open":100,"high":101,"low":97,"close":98}],-1)
        self.assertAlmostEqual(net_r,.9)
        self.assertEqual(reason,"target")

    def test_adverse_gap_not_filled_at_stop(self):
        net_r, _, reason = label_path(100,2,[{"open":95,"high":96,"low":94,"close":95}],1)
        self.assertAlmostEqual(net_r,-2.6)
        self.assertEqual(reason,"gap_stop")

    def test_timeout_uses_final_close(self):
        net_r, index, reason = label_path(100,2,[{"open":100,"high":101,"low":99,"close":100.5}],1)
        self.assertAlmostEqual(net_r,.15)
        self.assertEqual(reason,"timeout")

    def test_strategy_target_multiple(self):
        net_r, _, reason = label_path(100, 2, [{"open":100,"high":104,"low":99,"close":103}], 1, target_r=1.5)
        self.assertAlmostEqual(net_r,1.4)
        self.assertEqual(reason,"target")

    def test_indicator_prefix_is_unchanged_by_future_prices(self):
        import pandas as pd
        import numpy as np
        from apps.market_data.indicators import _with_indicator_columns
        close = 100 + np.sin(np.arange(100)/3)
        frame=pd.DataFrame({"open":close,"high":close+1,"low":close-1,"close":close,"volume":100})
        prefix=_with_indicator_columns(frame.iloc[:80].copy())
        frame.loc[80:,"close"] += 100
        full=_with_indicator_columns(frame.copy())
        for column in ["ema9","ema21","rsi","atr","adx","macd_hist","bb_width","sar"]:
            np.testing.assert_allclose(prefix[column],full[column].iloc[:80],equal_nan=True)
