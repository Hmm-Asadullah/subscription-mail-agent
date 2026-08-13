"""
Groups repeated billing emails from the same provider into a single
subscription record, keeping the earliest date as start_date and the
most recent as the current renewal_date/status.
"""

from collections import defaultdict


def deduplicate(subscriptions: list) -> list:
    groups = defaultdict(list)
    for s in subscriptions:
        groups[(s.provider, s.user_email)].append(s)

    merged = []
    for (_, _), items in groups.items():
        items.sort(key=lambda s: s.source_date)
        base = items[0]
        base.start_date = items[0].source_date

        latest = items[-1]
        base.renewal_date = latest.source_date
        base.status = latest.status
        base.price = latest.price or base.price

        merged.append(base)

    return merged