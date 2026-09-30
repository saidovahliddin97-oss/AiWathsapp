"""Scheduled GREETING_ONLY messages.

Every few minutes: inside the allowed hours, pick relatives nobody has talked
to for GREETING_INTERVAL_DAYS and greet them one by one with random pauses
(1-5 min by default) so it never looks like a mass mailing.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.webhook import Assistant

log = logging.getLogger("scheduler")
TICK_SECONDS = 600
MAX_PER_TICK = 3


def due_relatives(assistant: "Assistant", now: float | None = None, force_all: bool = False) -> list[str]:
    now = now or time.time()
    interval = assistant.settings.greeting_interval_days * 86400
    # Relatives with no history are counted from the day the bot was first started,
    # so launching the bot does not greet everybody at once.
    baseline = assistant.store.get_kv("greetings_baseline")
    if baseline is None:
        baseline = str(now)
        assistant.store.set_kv("greetings_baseline", baseline)
    due = []
    for rel in assistant.store.list_relatives():
        # greetings go only to people you listed, never to auto-added contacts or groups
        if rel.relation in ("family_group", "contact") or assistant.paused(rel.id) or assistant.blocked(rel.phone):
            continue
        if force_all:
            due.append(rel.id)
            continue
        last_contact = assistant.store.last_contact_at(rel.id) or float(baseline)
        if now - last_contact >= interval:
            due.append(rel.id)
    random.shuffle(due)
    return due


def in_window(assistant: "Assistant", now: float | None = None) -> bool:
    start, end = assistant.settings.greeting_window
    hour = time.localtime(now or time.time()).tm_hour
    return start <= hour < end


async def run_once(assistant: "Assistant", sleep=asyncio.sleep) -> int:
    """One scheduler tick. Returns how many greetings were sent."""
    store = assistant.store
    force_all = float(store.get_kv("greet_all_requested", "0")) > float(store.get_kv("greet_all_done", "0"))
    enabled = store.get_flag("greetings", assistant.settings.greetings_enabled)
    if not force_all and not (enabled and in_window(assistant)):
        return 0
    targets = due_relatives(assistant, force_all=force_all)
    if not force_all:
        targets = targets[:MAX_PER_TICK]
    sent = 0
    s = assistant.settings
    for i, rel_id in enumerate(targets):
        if i:
            await sleep(random.uniform(s.greeting_gap_minutes_min, s.greeting_gap_minutes_max) * 60)
        res = await assistant.greet(rel_id)
        log.info("scheduled greeting relative=%s status=%s", rel_id, res.status)
        if res.status in ("sent", "dry_run"):
            sent += 1
    if force_all:
        store.set_kv("greet_all_done", str(time.time()))
    return sent


async def loop(assistant: "Assistant") -> None:
    await asyncio.sleep(60)
    while True:
        try:
            await run_once(assistant)
        except Exception:
            log.exception("greeting scheduler error")
        await asyncio.sleep(TICK_SECONDS)
