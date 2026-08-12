"""
Broadcasts every newly-created TradingSignal to the "signals_live"
WebSocket group -- including NO_TRADE/rejected ones, not just approved
BUY signals, since the dashboard's "Signal Status" panel
(frontend/src/pages/Dashboard.jsx) is meant to show the latest signal
evaluation regardless of outcome, matching the "every decision must be
logged AND visible" principle applied to the live view, not just the
stored audit trail.
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import TradingSignal


@receiver(post_save, sender=TradingSignal)
def broadcast_new_signal(sender, instance: TradingSignal, created: bool, **kwargs):
    if not created:
        return

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

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
            },
        },
    )
