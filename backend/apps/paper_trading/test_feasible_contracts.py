from types import SimpleNamespace
from unittest.mock import patch
from datetime import timedelta
from django.test import SimpleTestCase
from django.utils import timezone
from .services.contract_selection import select_contract


class FeasibleContractTests(SimpleTestCase):
    def run_selection(self, approved, mode="ai"):
        expiry = timezone.localdate()+timedelta(days=7)
        preferred = {"contract_id":1,"in_sweet_spot":True,"score":1}
        other = {"contract_id":2,"in_sweet_spot":True,"score":.9}
        unsafe = {"contract_id":3,"in_sweet_spot":False,"score":2}
        quote = SimpleNamespace(ltp=100,bid=99,ask=100,open_interest=1000,volume=1000,age_seconds=1)
        with patch("apps.options.models.get_options_strategy_settings", return_value=SimpleNamespace(strike_mode=mode)), \
             patch("apps.options.expiry_service.resolve_current_expiry",return_value=expiry), \
             patch("apps.options.expiry_service.is_expiry_eligible",return_value=True), \
             patch("apps.options.strike_selector.suggest_best_strike",return_value={"suggested":preferred,"candidates":[preferred,other,unsafe],"reason":"preferred"}), \
             patch("apps.options.models.OptionContract.objects.get",side_effect=lambda pk: SimpleNamespace(pk=pk,is_active=True,symbol_token="valid",lot_size=65)), \
             patch("apps.paper_trading.services.market_data_reader.latest_contract_quote",return_value=quote), \
             patch("apps.paper_trading.services.paper_risk_engine.check_pre_entry",side_effect=lambda account,contract,**kwargs: SimpleNamespace(approved=contract.pk in approved,reason_text="blocked")) as gate:
            candidate,reason=select_contract("NIFTY","CE",account=SimpleNamespace(pk=1))
            self.assertTrue(all(call.args[1].pk != 3 for call in gate.call_args_list))
            return candidate,reason,gate.call_count

    def test_preferred_remains_first(self):
        candidate,_,count=self.run_selection({1,2})
        self.assertEqual(candidate.contract.pk,1)
        self.assertEqual(count,1)

    def test_feasible_alternative_can_qualify(self):
        candidate,_,count=self.run_selection({2})
        self.assertEqual(candidate.contract.pk,2)
        self.assertEqual(count,2)

    def test_no_order_when_all_candidates_blocked(self):
        candidate,reason,_=self.run_selection(set())
        self.assertIsNone(candidate)
        self.assertEqual(reason,"blocked")

    def test_explicit_strike_mode_is_preserved(self):
        candidate,_,count=self.run_selection({2}, mode="atm")
        self.assertIsNone(candidate)
        self.assertEqual(count,1)
