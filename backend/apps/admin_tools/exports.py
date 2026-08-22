"""
manual 14.15 "Export Trades" automation command. Writes a real CSV of
closed OpenPosition rows to disk and returns the path + row count --
same "report nothing beats reporting a fabricated number" principle
the rest of this codebase uses (apps/analytics/services.py,
apps/jarvis/responses.py): if there are zero closed trades, this says
so and doesn't write an empty file that looks like a stale success.
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.conf import settings
from django.utils import timezone

EXPORT_DIR = settings.BASE_DIR / "exports"


def export_closed_trades_csv() -> tuple[Path | None, int]:
    from apps.execution.models import OpenPosition

    closed = OpenPosition.objects.filter(closed_at__isnull=False).order_by("-closed_at")
    count = closed.count()
    if count == 0:
        return None, 0

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = timezone.localtime(timezone.now()).strftime("%Y%m%d_%H%M%S")
    path = EXPORT_DIR / f"closed_trades_{timestamp}.csv"

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "symbol", "side", "qty", "entry_price", "stop_loss", "target_price",
            "exit_pnl", "opened_at", "closed_at",
        ])
        for p in closed:
            writer.writerow([
                p.symbol, p.side, p.qty, p.entry_price, p.stop_loss, p.target_price,
                p.unrealized_pnl, p.opened_at, p.closed_at,
            ])

    return path, count
