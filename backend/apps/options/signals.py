"""
Broadcasts every newly-created OptionChainSnapshot to the
"options_live" WebSocket group (apps/options/consumers.py). Same
pattern as apps/market_data/signals.py -- a post_save signal, not a
call from apps.options.tasks.ingest_option_chain_snapshots directly, so
any code path that creates a snapshot (the recurring task, a manual
shell one-off) automatically pushes a live update.
"""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from common.websockets import broadcast_group

from .models import OptionChainSnapshot

logger = logging.getLogger(__name__)


@receiver(post_save, sender=OptionChainSnapshot)
def broadcast_new_snapshot(sender, instance: OptionChainSnapshot, created: bool, **kwargs):
    if not created:
        return

    contract = instance.contract

    greeks_payload = None
    if instance.iv is not None:
        try:
            from .greeks import compute_greeks
            from .signals_engine import _latest_underlying_ltp

            spot = _latest_underlying_ltp(contract.underlying, contract.expiry)
            if spot is not None:
                from django.utils import timezone as _tz
                tte_years = (contract.expiry - _tz.localdate()).days / 365.0
                greeks_payload = compute_greeks(
                    spot, float(contract.strike), tte_years, 0.065,
                    instance.iv / 100.0, contract.option_type,
                )
        except Exception:
            greeks_payload = None  # never let a Greeks calc failure block the live price push

    broadcast_group(
        "options_live",
        {
            "type": "chain_update",  # must match the consumer method name exactly
            "data": {
                "contract_id": contract.id,
                "underlying": contract.underlying,
                "expiry": contract.expiry.isoformat(),
                "strike": float(contract.strike),
                "option_type": contract.option_type,
                "timestamp": instance.timestamp.isoformat(),
                "ltp": float(instance.ltp),
                "open_interest": instance.open_interest,
                "change_in_oi": instance.change_in_oi,
                "volume": instance.volume,
                "iv": instance.iv,
                "bid": float(instance.bid) if instance.bid is not None else None,
                "ask": float(instance.ask) if instance.ask is not None else None,
                "greeks": greeks_payload,
            },
        },
        log=logger,
    )
