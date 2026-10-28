import numpy as np
import pandas as pd
from django.test import SimpleTestCase
from .scalp_return_training import summarize, choose_cutoff


class ReturnSelectionTests(SimpleTestCase):
    def frame(self,value):
        return pd.DataFrame({"day":[str(i//10) for i in range(200)],"net_r":[value]*200})

    def test_losing_setups_do_not_pass_selection_gate(self):
        best,_=choose_cutoff(self.frame(-.1),np.ones(200))
        self.assertIsNone(best)

    def test_many_positive_days_pass_validation_gate(self):
        best,_=choose_cutoff(self.frame(.2),np.ones(200))
        self.assertIsNotNone(best)
        self.assertEqual(best["metrics"]["count"],200)

    def test_small_selection_is_not_accepted(self):
        predictions=np.zeros(200)-1
        predictions[:20]=1
        best,_=choose_cutoff(self.frame(.2),predictions)
        self.assertIsNone(best)

    def test_empty_selection_has_no_invented_returns(self):
        result=summarize(self.frame(.2),np.zeros(200,dtype=bool))
        self.assertEqual(result["count"],0)
        self.assertIsNone(result["mean_net_r"])
        self.assertIsNone(result["daily_mean_ci95"])
