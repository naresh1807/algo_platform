"""
Broadcasts every newly-created TradingSignal to the "signals_live"
WebSocket group -- including NO_TRADE/rejected ones, not just approved
BUY signals, since the dashboard's "Signal Status" panel
(frontend/src/pages/Dashboard.jsx) is meant to show the latest signal
evaluation regardless of outcome, matching the "every decision must be
logged AND visible" principle applied to the live view, not just the
stored audit trail.
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import TradingSignal

logger = logging.getLogger(__name__)


@receiver(post_save, sender=TradingSignal)
def broadcast_new_signal(sender, instance: TradingSignal, created: bool, **kwargs):
    if not created:
        return

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    # The row is already committed to the DB at this point (that's the
    # part generate_signal()'s "always returns a saved TradingSignal"
    # guarantee depends on) -- this broadcast is a best-effort side
    # channel for the live dashboard on top of that, so a transient
    # channel-layer/Redis failure here must never bubble up and be
    # mistaken for a signal-generation failure by callers like
    # apps.signals.tasks.generate_signals_for_watchlist.
    try:
        async_to_sync(channel_layer.group_send)(
            "signals_live",
            {
                "type": "signal_update",
                "data": {
                    "id": instance.pk,
                    "symbol": instance.symbol,
                    "signal_type": instance.signal_type,
                    "status": instance.status,
                    "total_score": instance.total_score,
                    "technical_score": instance.technical_score,
                    "sentiment_score": instance.sentiment_score,
                    "regime": instance.regime,
                    "reason": instance.reason,
                    "created_at": instance.created_at.isoformat(),
                    # CE/PE + strike -- only set by apps.options.
                    # index_direction_strategy's APPROVED signals (null for
                    # every equity/index-engine signal, which doesn't trade
                    # an option contract) so the live popup/dashboard can
                    # show "which strike, CE or PE" without parsing `reason`.
                    "option_side": instance.option_side,
                    "strike_price": (
                        str(instance.strike_price) if instance.strike_price is not None else None
                    ),
                    # Trade levels -- added so a live "positive signal" popup
                    # (frontend/src/components/SignalAlertPopup.jsx) can show
                    # actionable entry/stop/target numbers immediately, without
                    # a second REST round-trip keyed off `id`. str() to match
                    # every other Decimal-over-the-wire field in this codebase
                    # (e.g. apps/investing/signals.py's `change`).
                    "entry_price": (
                        str(instance.entry_price)
                        if instance.entry_price is not None
                        else None
                    ),
                    "stop_loss": (
                        str(instance.stop_loss) if instance.stop_loss is not None else None
                    ),
                    "target_1": (
                        str(instance.target_1) if instance.target_1 is not None else None
                    ),
                    "target_2": (
                        str(instance.target_2) if instance.target_2 is not None else None
                    ),
                    # Added so the live signal card/terminal doesn't need a
                    # second REST round-trip to show confidence, the
                    # options-chain confirmation score, or which stage a
                    # rejected signal died at.
                    "option_contract_id": instance.option_contract_id,
                    "ml_win_probability": instance.ml_win_probability,
                    "options_score": instance.options_score,
                    "rejection_stage": instance.rejection_stage,
                },
            },
        )
    except Exception:
        logger.exception(
            "Failed to broadcast TradingSignal %s to signals_live -- signal was still saved.",
            instance.pk,
        )
