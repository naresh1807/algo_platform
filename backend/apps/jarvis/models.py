from django.conf import settings
from django.db import models


class JarvisMemory(models.Model):
    """
    manual section 15.17: jarvis_memory. Per-user context JARVIS carries
    between commands within (and briefly across) a session -- current
    page, active strategy, last command, user preferences.

    One row per user (not a true singleton like AccountEquity/
    KillSwitchState) -- version 2.0 is explicitly multi-user (manual
    1.12), so this is built as per-user from day one rather than a
    singleton that would need retrofitting later.

    manual 14.17 is explicit that only "necessary settings" survive a
    session, not the full conversation -- `context_json` is deliberately
    a small, overwritten-in-place blob (current_page, active_strategy,
    last_command, preferences), not an ever-growing transcript. The
    transcript itself lives in JarvisCommandHistory below, which is
    append-only and queried, not loaded whole into a prompt.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="jarvis_memory",
    )
    context_json = models.JSONField(
        default=dict,
        help_text="Small, overwritten-in-place: current_page, active_strategy, "
                  "last_command, last_intent, preferences. Not a transcript.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "jarvis_memory"

    def __str__(self):
        return f"JarvisMemory({self.user})"


class JarvisCommandHistory(models.Model):
    """
    manual section 15.18: jarvis_command_history. Every command JARVIS
    receives is logged here -- voice or typed text, the intent that was
    detected, which module actually handled it, the result, and how
    long it took. This is what apps.admin_tools.audit.AuditLog is for
    governance-relevant actions in general; this table is JARVIS-
    specific and always written (even for a plain "show portfolio"
    read), since manual 14.25 principle #5 is "Explain Every Action" --
    that requires a full record of every action, not just the
    consequential ones.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="jarvis_commands",
    )
    raw_text = models.TextField(help_text="The command exactly as received (voice-transcribed or typed).")
    input_mode = models.CharField(
        max_length=16, default="text",
        help_text="voice | text | chat -- manual 14.6 Input Modes.",
    )
    intent_category = models.CharField(max_length=32, blank=True)
    intent_action = models.CharField(max_length=64, blank=True)
    executed_module = models.CharField(
        max_length=64, blank=True,
        help_text="Which apps.jarvis handler (or downstream app) actually ran, e.g. 'risk.get_equity'.",
    )
    result_text = models.TextField(blank=True)
    success = models.BooleanField(default=True)
    needs_confirmation = models.BooleanField(
        default=False,
        help_text="True when this command was a restricted action (manual 14.21) "
                  "and JARVIS asked for confirmation instead of executing it.",
    )
    execution_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "jarvis_command_history"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"]), models.Index(fields=["intent_category"])]

    def __str__(self):
        return f"[{self.intent_category or '?'}] {self.raw_text[:60]}"


class MarketOutlookSnapshot(models.Model):
    """
    apps.jarvis.market_intelligence's own output, kept as history
    rather than only computed on demand -- lets the frontend/JARVIS
    show "how has the outlook been trending today" instead of only the
    latest read, and lets apps.jarvis.tasks.generate_market_outlook
    (periodic, market-hours-only) compare the new outlook against the
    previous one to decide whether a bias CHANGE is worth a JARVIS
    announcement (manual 14.16-style), rather than announcing the same
    "Neutral" read every 15 minutes all day.

    Deliberately does NOT introduce any new prediction of its own --
    every field here is read from apps.signals/apps.news/apps.options/
    apps.investing's OWN already-computed output (see
    market_intelligence.py's module docstring). This table exists to
    give that synthesis a queryable, timestamped record, not to add a
    new model.
    """

    generated_at = models.DateTimeField(auto_now_add=True, db_index=True)
    bias = models.CharField(max_length=16, help_text="Bullish / Bearish / Neutral")
    bias_confidence = models.FloatField(null=True, blank=True, help_text="0.0-1.0, when derivable.")
    summary = models.TextField(help_text="The full explained narrative -- what JARVIS actually says.")
    best_options_idea = models.CharField(max_length=255, blank=True)
    best_stock_idea = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "market_outlook_snapshots"
        ordering = ["-generated_at"]

    def __str__(self):
        return f"{self.bias} @ {self.generated_at}"
