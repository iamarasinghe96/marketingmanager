from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def local_day(campaign, now):
    return now.astimezone(ZoneInfo(campaign.timezone)).date()


def local_slot(campaign, day, clock):
    return datetime.combine(day, time.fromisoformat(clock), ZoneInfo(campaign.timezone)).astimezone(timezone.utc)


def staggered_slots(campaigns, day_by_campaign, gap_minutes=30):
    """Resolve times in UTC, including Sydney DST and Colombo's half-hour offset.

    Build all neighbouring days to keep collision decisions deterministic on restarts.
    Only move times later. Return the selected local days, never outside their day.
    """
    events = []
    for c in campaigns.values():
        if not c.enabled:
            continue
        for offset in (-1, 0, 1):
            day = day_by_campaign[c.slug] + timedelta(days=offset)
            for kind, clock in (("post",c.post_time),("story",c.story_time)):
                events.append((local_slot(c,day,clock),c.slug,kind,day))
    events.sort()
    last = None
    result = {}
    for due, slug, kind, day in events:
        if last and due - last < timedelta(minutes=gap_minutes):
            due = last + timedelta(minutes=gap_minutes)
        if local_day(campaigns[slug],due) != day:
            raise ValueError(f"{slug}: publishing times and 30-minute gap run past midnight")
        if day == day_by_campaign[slug]:
            result[(slug,kind)] = due
        last = due
    return result


def slots_for(campaigns, campaign, day, gap_minutes=30):
    anchor = local_slot(campaign,day,"12:00")
    days = {c.slug: local_day(c,anchor) for c in campaigns.values()}
    days[campaign.slug] = day
    slots = staggered_slots(campaigns,days,gap_minutes)
    return slots[(campaign.slug,"post")], slots[(campaign.slug,"story")]


def next_free_day(store, campaign, now, campaigns, gap=30):
    day = local_day(campaign,now)
    for _ in range(367):
        existing = store.one("SELECT id FROM drafts WHERE campaign=? AND day=?", (campaign.slug,day.isoformat()))
        post_at, _ = slots_for(campaigns,campaign,day,gap)
        if not existing and (day > local_day(campaign,now) or now < post_at):
            return day
        day += timedelta(days=1)
    raise ValueError("No free slot in the next year")


def choose_layouts(campaign, history):
    """At least 48 compositions per format; avoid repeating a base consecutively."""
    result = []
    for kind, layouts, variants in (("feed",campaign.feed_layouts,8),("story",campaign.story_layouts,16)):
        last = history[0].get(f"{kind}_layout") if history else None
        seen = {(h.get(f"{kind}_layout"),h.get(f"{kind}_variant")) for h in history}
        choices = [(layout,variant) for variant in range(variants) for layout in layouts
                   if layout != last and (layout,variant) not in seen]
        if not choices:
            choices = [(layout,variant) for variant in range(variants) for layout in layouts if layout != last]
        index = len(history) % len(choices)
        result.append(choices[index])
    return result[0][0], result[1][0], result[0][1], result[1][1]
