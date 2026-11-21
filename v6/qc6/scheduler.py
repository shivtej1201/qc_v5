"""Schedules for checks and journeys (blueprint §8.1). Plain-language schedule strings:

    daily 06:30                 every day at 06:30 in the site's time zone
    weekdays 07:15              Monday to Friday
    mondays 09:00               one weekday (mondays … sundays)
    every 6 hours               fixed interval from the last run
    off                         no schedule

The worker calls `tick()` once a minute. A schedule fires when its next time has passed since it last fired; a site
never gets a second run while one is queued, running or paused (the check waits for the next slot).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

DAYS = ["mondays", "tuesdays", "wednesdays", "thursdays", "fridays", "saturdays", "sundays"]
_AT = re.compile(r"^(daily|weekdays|weekends|mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays)\s+(\d{1,2}):(\d{2})$")
_EVERY = re.compile(r"^every\s+(\d{1,3})\s+(minutes?|hours?)$")


class ScheduleError(ValueError):
    pass


def parse(text: Optional[str]) -> Optional[Dict[str, Any]]:
    t = (text or "").strip().lower()
    if not t or t == "off":
        return None
    m = _AT.match(t)
    if m:
        hh, mm = int(m.group(2)), int(m.group(3))
        if hh > 23 or mm > 59:
            raise ScheduleError("Use a time like 06:30 (24-hour clock).")
        kind = m.group(1)
        days = {"daily": list(range(7)), "weekdays": list(range(5)), "weekends": [5, 6]}.get(kind, [DAYS.index(kind)] if kind in DAYS else [])
        return {"type": "at", "days": days, "hour": hh, "minute": mm}
    m = _EVERY.match(t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        minutes = n * (60 if unit.startswith("hour") else 1)
        if minutes < 60:
            raise ScheduleError("Checks can run at most once an hour.")
        return {"type": "every", "minutes": minutes}
    raise ScheduleError('Write the schedule like "daily 06:30", "weekdays 07:15", "mondays 09:00" or "every 6 hours".')


def next_due(spec: Dict[str, Any], last: datetime, tz: str = "Asia/Kolkata") -> datetime:
    """The first scheduled time strictly after `last` (UTC)."""
    if spec["type"] == "every":
        return last + timedelta(minutes=spec["minutes"])
    zone = ZoneInfo(tz)
    local_last = last.astimezone(zone)
    d = local_last.replace(hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0)
    if d <= local_last:
        d += timedelta(days=1)
    while d.weekday() not in spec["days"]:
        d += timedelta(days=1)
    return d.astimezone(timezone.utc)


async def tick(pool, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Fire due schedules. Returns what was started (for logs and tests)."""
    from qc6 import services
    now = now or datetime.now(timezone.utc)
    started = []
    sites = await pool.fetch("SELECT id, settings FROM sites")
    for s in sites:
        st = s["settings"] or {}
        tz = st.get("timezone", "Asia/Kolkata")
        jobs = []
        try:
            spec = parse(st.get("check_schedule"))
        except ScheduleError:
            spec = None
        if spec:
            jobs.append((f"site:{s['id']}:check", spec, "full", {}))
        for j in await pool.fetch("SELECT id, spec FROM journeys WHERE site_id = $1 AND enabled", s["id"]):
            try:
                jspec = parse((j["spec"] or {}).get("schedule"))
            except ScheduleError:
                jspec = None
            if jspec:
                jobs.append((f"journey:{j['id']}", jspec, "journey", {"journey_ids": [j["id"]]}))
        for key, spec, kind, opts in jobs:
            last = await pool.fetchval("SELECT last_fired_at FROM schedule_state WHERE key = $1", key)
            if last is None:                 # first time we see this schedule: start counting from now
                await pool.execute("INSERT INTO schedule_state (key, last_fired_at) VALUES ($1, $2) ON CONFLICT DO NOTHING", key, now)
                continue
            due = next_due(spec, last, tz)
            if due > now:
                continue
            try:
                rid = await services.start_run(pool, s["id"], kind, {**opts, "scheduled": True})
            except services.NotAllowed:
                continue                     # busy: try again next tick
            await pool.execute("INSERT INTO schedule_state (key, last_fired_at) VALUES ($1, $2) "
                               "ON CONFLICT (key) DO UPDATE SET last_fired_at = EXCLUDED.last_fired_at", key, now)
            started.append({"key": key, "run_id": rid, "kind": kind})
    return started
