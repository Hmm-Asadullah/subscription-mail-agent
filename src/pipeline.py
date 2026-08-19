"""
Core extraction pipeline. Callable from either the CLI (main.py) or the web
app (web_app.py). Takes already-obtained credentials and returns a
list of Row objects representing CURRENTLY ACTIVE subscriptions only —
canceled subscriptions and free trials that never converted are
filtered out, even if they matched the search queries.
"""

import os
import base64
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from collections import defaultdict

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from gmail_client import GmailClient, SUBSCRIPTION_QUERIES
from filters import is_likely_subscription
from parser import extract_price, extract_dates, extract_status, extract_provider
from normalize import normalize_provider

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# If unset, scans the ENTIRE mailbox with no date cutoff. Set
# SEARCH_AFTER_DATE (format YYYY/MM/DD) as an env var to limit the scan
# to recent history instead.
SEARCH_AFTER = os.environ.get("SEARCH_AFTER_DATE", "")

# Max messages fetched PER search query (there are 4 queries, so total
# possible messages fetched is up to 4x this, before dedup).
MAX_RESULTS_PER_QUERY = int(os.environ.get("MAX_RESULTS_PER_QUERY", "5000"))


@dataclass
class Row:
    provider: str
    start_date: str
    end_date: str
    amount: float
    currency: str
    reason: str
    status: str
    source_email_subject: str
    source_email_date: str


def get_body_text(payload) -> str:
    parts = payload.get("parts", [payload])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part["body"].get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        if part.get("mimeType") == "text/html":
            data = part["body"].get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                return BeautifulSoup(html, "html.parser").get_text(" ")
    return ""


def _parse_email_date(date_str: str):
    """Parses an email Date header into a sortable datetime. Falls back
    to the minimum possible date if parsing fails, so malformed dates
    sort first rather than crashing the sort."""
    try:
        return parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        import datetime
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def merge_to_current_subscriptions(rows: list) -> list:
    """
    Collapses multiple emails from the same provider (e.g. 12 monthly
    Netflix receipts) into a single row representing that subscription's
    CURRENT state, then keeps only subscriptions whose latest known
    status is "active" — free trials that never converted and anything
    canceled are dropped from the result.

    "Current state" is determined by the chronologically most recent
    matching email for that provider (by actual email send date), not
    by dates mentioned inside the email body.
    """
    groups = defaultdict(list)
    for row in rows:
        groups[row.provider].append(row)

    merged = []
    for provider, group in groups.items():
        group.sort(key=lambda r: _parse_email_date(r.source_email_date))
        earliest = group[0]
        latest = group[-1]

        if latest.status != "active":
            continue  # drop canceled and trial-only subscriptions

        merged.append(Row(
            provider=provider,
            start_date=earliest.start_date or earliest.source_email_date,
            end_date=latest.end_date or latest.source_email_date,
            amount=latest.amount,
            currency=latest.currency,
            reason=latest.reason,
            status=latest.status,
            source_email_subject=latest.source_email_subject,
            source_email_date=latest.source_email_date,
        ))

    return merged


def run_pipeline(
    creds,
    search_after: str = SEARCH_AFTER,
    search_before: str = "",
    max_results: int = MAX_RESULTS_PER_QUERY,
) -> list:
    client = GmailClient(creds)
    rows = []
    seen_ids = set()

    all_msg_ids = []
    for query in SUBSCRIPTION_QUERIES:
        full_query = query
        if search_after:
            full_query += f" after:{search_after}"
        if search_before:
            full_query += f" before:{search_before}"
        for msg_ref in client.search(full_query, max_results=max_results):
            msg_id = msg_ref["id"]
            if msg_id not in seen_ids:
                seen_ids.add(msg_id)
                all_msg_ids.append(msg_id)

    print(f"Found {len(all_msg_ids)} unique candidate messages. Fetching in batches...")

    for msg_id, msg in client.get_messages_batch(all_msg_ids):
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        subject = headers.get("Subject", "")
        sender = headers.get("From", "")
        body = get_body_text(msg["payload"])
        snippet = msg.get("snippet", "")

        if not is_likely_subscription(subject, snippet, body, sender):
            continue

        amount, currency = extract_price(f"{subject} {body}")
        dates = extract_dates(body)
        status = extract_status(f"{subject} {body}")
        sender_name = sender.split("<")[0].strip()
        raw_provider = extract_provider(sender, sender_name)
        provider = normalize_provider(raw_provider, sender)

        rows.append(Row(
            provider=provider,
            start_date=str(dates[0]) if dates else "",
            end_date=str(dates[-1]) if dates else "",
            amount=amount or 0.0,
            currency=currency or "",
            reason="unknown",
            status=status,
            source_email_subject=subject,
            source_email_date=headers.get("Date", ""),
        ))

    return merge_to_current_subscriptions(rows)