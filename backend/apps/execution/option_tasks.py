"""Frequent exits for operator-entered options, independent of entry generation."""
import logging

from celery import shared_task

from apps.market_data.market_hours import is_market_open
from apps.risk.engine import is_kill_switch_active
from .models import OpenPosition
from .option_trading import current_quote
from .trailing_stop import update_trailing_stop

logger = logging.getLogger(__name__)


@shared_task
def monitor_manual_option_positions():
    if not is_market_open()[0]:
        return []
    results = []
    halt = is_kill_switch_active()
    positions = OpenPosition.objects.filter(
        closed_at__isnull=True, option_contract__isnull=False,
        signal__optiontraderequest__isnull=False,
    ).select_related("option_contract").distinct()
    for position in positions:
        try:
            quote = current_quote(position.option_contract)
            price = quote.bid if quote.bid is not None and quote.bid > 0 else quote.ltp
            update_trailing_stop(position, price)
            reason = None
            if halt:
                reason = "Kill switch active"
            elif price <= position.stop_loss:
                reason = "Option stop-loss hit"
            elif position.target_price is not None and price >= position.target_price:
                reason = "Option target hit"
            if reason:
                if position.execution_mode == "paper":
                    from .paper_executor import close_position
                    closed = close_position(position, price, reason)
                else:
                    from .live_executor import close_position_live
                    close_position_live(position, reason)
                    position.refresh_from_db()
                    closed = not position.is_open
                results.append({"position_id": position.pk, "closed": closed, "reason": reason})
            elif position.execution_mode == "paper":
                from .paper_executor import mark_to_market
                mark_to_market(position, price)
        except Exception:
            # A stale quote or unresolved broker exit must not prevent another
            # independent position from receiving its own exit check.
            logger.exception("Manual option exit check failed for position %s", position.pk)
    return results

