"""
Celery tasks for apps.admin_tools. Currently just the async wrapper
around `backup_database` (manual 14.15) -- mysqldump can take a while
on a real database, so both the nightly beat schedule (config/celery.py)
and JARVIS's on-demand "backup database" command (apps/jarvis/responses.py)
go through this task rather than running the management command inline
in a request/response cycle.
"""

from __future__ import annotations

from celery import shared_task
from django.core.management import call_command


@shared_task
def run_database_backup() -> str:
    call_command("backup_database")
    return "backup_database management command completed"
