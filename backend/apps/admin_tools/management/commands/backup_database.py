"""
manual 14.15 "Backup Database" automation command. Shells out to
`mysqldump` (matching manual: "MySQL is the system of record" -- see
config/settings.py's own note on why SQLite is never used, even for
dev) rather than a Django-level fixture dump, since a real disaster-
recovery backup needs the actual SQL dump `mysql` can restore from
directly, not a JSON serialization that only round-trips through
Django's own loaddata.

Usage:
    python manage.py backup_database
    python manage.py backup_database --output-dir /path/to/backups

Requires the `mysqldump` binary to be on PATH (it ships with any MySQL
client install; the docker/docker-compose.yml MySQL image has it too).
Honest failure mode: if mysqldump isn't installed or the connection
fails, this raises CommandError with the real subprocess error rather
than reporting a fake success.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.admin_tools.audit import log_action

DEFAULT_BACKUP_DIR = settings.BASE_DIR / "backups"


class Command(BaseCommand):
    help = "Back up the MySQL database via mysqldump. Used by JARVIS's 'backup database' command."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir", default=str(DEFAULT_BACKUP_DIR),
            help=f"Directory to write the .sql dump to (default: {DEFAULT_BACKUP_DIR}).",
        )

    def handle(self, *args, **options):
        db = settings.DATABASES["default"]
        output_dir = Path(options["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = timezone.localtime(timezone.now()).strftime("%Y%m%d_%H%M%S")
        dump_path = output_dir / f"{db['NAME']}_{timestamp}.sql"

        cmd = [
            "mysqldump",
            f"--host={db['HOST']}",
            f"--port={db['PORT']}",
            f"--user={db['USER']}",
            "--single-transaction",  # consistent snapshot without locking tables (InnoDB)
            "--routines",
            "--triggers",
            db["NAME"],
        ]
        env = {"MYSQL_PWD": db["PASSWORD"]} if db.get("PASSWORD") else {}

        try:
            with open(dump_path, "wb") as f:
                result = subprocess.run(
                    cmd, stdout=f, stderr=subprocess.PIPE, env=env, timeout=600,
                )
        except FileNotFoundError as exc:
            raise CommandError(
                "mysqldump not found on PATH -- install the MySQL client tools first."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError("mysqldump timed out after 600s.") from exc

        if result.returncode != 0:
            dump_path.unlink(missing_ok=True)
            raise CommandError(f"mysqldump failed: {result.stderr.decode(errors='replace')[:500]}")

        size_kb = dump_path.stat().st_size / 1024

        log_action(
            action="database_backup_completed",
            actor_label="management_command:backup_database",
            details={"path": str(dump_path), "size_kb": round(size_kb, 1)},
        )

        # Best-effort JARVIS announcement (manual 14.16) -- lazy-imported
        # so this command has no hard dependency on apps.jarvis / Channels
        # being importable in every environment this runs in.
        try:
            from apps.jarvis.notify import announce
            announce(
                "database_backup_completed",
                f"Database backup completed: {dump_path.name} ({size_kb:.0f} KB).",
                path=str(dump_path),
            )
        except Exception:
            pass

        self.stdout.write(self.style.SUCCESS(f"Backup written to {dump_path} ({size_kb:.0f} KB)."))
