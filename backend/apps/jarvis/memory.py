"""
Context Memory (manual 14.17, 14.23 priority #8).

Deliberately tiny: get_or_create + a shallow dict merge. The manual is
explicit that only "necessary settings" survive between commands, not
a growing conversation transcript (that's what JarvisCommandHistory is
for, and it's queried on demand, not loaded into memory whole).
"""

from __future__ import annotations

from .models import JarvisMemory

DEFAULT_CONTEXT = {
    "current_page": "/",
    "active_strategy": None,
    "last_command": None,
    "last_intent": None,
}


def get_memory(user) -> JarvisMemory:
    memory, _ = JarvisMemory.objects.get_or_create(
        user=user, defaults={"context_json": dict(DEFAULT_CONTEXT)},
    )
    return memory


def update_context(user, **updates) -> JarvisMemory:
    memory = get_memory(user)
    context = {**DEFAULT_CONTEXT, **memory.context_json, **updates}
    memory.context_json = context
    memory.save(update_fields=["context_json", "updated_at"])
    return memory
