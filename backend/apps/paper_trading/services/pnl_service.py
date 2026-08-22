"""
PaperPnLService -- versioned charge calculation (PaperChargeRuleSet),
gross/net P&L, and the append-only PaperLedger. Every cash movement is
applied to PaperAccount incrementally (never as one lump sum) so each
PaperLedger row's `balance_after` genuinely reflects the account's
balance at that exact point -- required for the ledger to actually
reconcile against the account (spec test: "Account balance and ledger
reconcile"), not just be a decorative log.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from ..models import PaperChargeRuleSet, PaperLedger, PaperTrade

MONEY_QUANTUM = Decimal("0.01")


def latest_charge_rule_set(as_of_date):
    return PaperChargeRuleSet.objects.filter(effective_from__lte=as_of_date).order_by("-effective_from").first()


def compute_charges(rule_set: PaperChargeRuleSet, *, entry_price: Decimal, exit_price: Decimal, quantity: int) -> dict:
    turnover_buy = entry_price * quantity
    turnover_sell = exit_price * quantity
    brokerage = (rule_set.brokerage_flat_per_order * 2).quantize(MONEY_QUANTUM)
    stt = (turnover_sell * rule_set.stt_pct_of_premium / 100).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    exchange_charges = ((turnover_buy + turnover_sell) * rule_set.exchange_txn_pct / 100).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    gst = ((brokerage + exchange_charges) * rule_set.gst_pct_of_brokerage_and_txn / 100).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    sebi_charges = ((turnover_buy + turnover_sell) * rule_set.sebi_charges_pct / 100).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    stamp_duty = (turnover_buy * rule_set.stamp_duty_pct_buy_side / 100).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    other_charges = (rule_set.other_charges_flat_per_order * 2).quantize(MONEY_QUANTUM)
    return {
        "brokerage": brokerage, "stt": stt, "exchange_charges": exchange_charges, "gst": gst,
        "sebi_charges": sebi_charges, "stamp_duty": stamp_duty, "other_charges": other_charges,
    }


def roll_daily_baseline_if_needed(account) -> None:
    """Same rollover shape as apps.risk.engine.get_equity's daily-baseline
    logic -- a new trading day resets daily_start_equity/daily_drawdown
    to today's opening equity WITHOUT touching current_equity/realized_pnl
    (spec: no automatic reset of the main account balance, only the
    per-day drawdown baseline)."""
    today = timezone.localdate()
    if account.trading_day == today:
        return
    account.trading_day = today
    account.daily_start_equity = account.current_equity
    account.daily_drawdown = Decimal("0")
    account.save(update_fields=["trading_day", "daily_start_equity", "daily_drawdown"])


def _recompute_equity_and_drawdown(account) -> None:
    account.current_equity = account.available_cash + account.used_capital + account.reserved_capital
    if account.current_equity > account.peak_equity:
        account.peak_equity = account.current_equity
    if account.peak_equity > 0:
        drawdown_pct = (account.peak_equity - account.current_equity) / account.peak_equity * 100
        if drawdown_pct > account.max_drawdown:
            account.max_drawdown = drawdown_pct
    if account.daily_start_equity and account.daily_start_equity > 0:
        daily_dd = (account.daily_start_equity - account.current_equity) / account.daily_start_equity * 100
        account.daily_drawdown = max(Decimal("0"), daily_dd)


def settle_trade(account, position, exit_order, reason: str) -> PaperTrade:
    roll_daily_baseline_if_needed(account)

    entry_price = position.average_entry_price
    exit_price = exit_order.average_fill_price
    quantity = position.quantity
    gross_pnl = ((exit_price - entry_price) * quantity).quantize(MONEY_QUANTUM)

    rule_set = latest_charge_rule_set(timezone.localdate())
    if rule_set is None:
        raise RuntimeError(
            "No PaperChargeRuleSet configured -- run `python manage.py seed_paper_charge_rules` "
            "before any trade can be settled. See that command's docstring for rate sourcing/update notes."
        )
    charges = compute_charges(rule_set, entry_price=entry_price, exit_price=exit_price, quantity=quantity)

    from django.conf import settings as dj_settings
    slippage_rate = Decimal(str(dj_settings.AI_PAPER_SLIPPAGE_BPS)) / Decimal(10000)
    slippage_cost = ((entry_price + exit_price) * quantity * slippage_rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)

    total_charges = sum(charges.values()) + slippage_cost
    net_pnl = gross_pnl - total_charges

    trade = PaperTrade.objects.create(
        position=position, entry_order=position.entry_order, exit_order=exit_order,
        entry_price=entry_price, exit_price=exit_price, quantity=quantity, gross_pnl=gross_pnl,
        slippage_cost=slippage_cost, net_pnl=net_pnl, charge_rule_version=rule_set.version,
        fill_model_version=exit_order.fill_model_version, closed_at=position.closed_at,
        **charges,
    )

    # Step 1: release the position's reserved cost basis and credit the
    # gross exit proceeds -- one ledger row, one incremental balance.
    account.used_capital -= entry_price * quantity
    account.available_cash += exit_price * quantity
    account.save(update_fields=["used_capital", "available_cash"])
    PaperLedger.objects.create(
        account=account, entry_type=PaperLedger.EntryType.CAPITAL_RELEASE, amount=exit_price * quantity,
        balance_after=account.available_cash, reference_type="paper_trade", reference_id=str(trade.pk),
    )

    # Step 2: debit charges -- a SEPARATE ledger row/balance so the ledger
    # shows exactly what happened, in order, not one blended delta.
    account.available_cash -= total_charges
    account.total_charges += total_charges
    account.gross_pnl += gross_pnl
    account.realized_pnl += net_pnl
    account.net_pnl += net_pnl
    _recompute_equity_and_drawdown(account)
    account.save()
    PaperLedger.objects.create(
        account=account, entry_type=PaperLedger.EntryType.CHARGE, amount=-total_charges,
        balance_after=account.available_cash, reference_type="paper_trade", reference_id=str(trade.pk),
    )

    return trade
