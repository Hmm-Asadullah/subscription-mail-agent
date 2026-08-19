"""
Core extraction pipeline, callable from either the CLI (main.py) or
the web app (web_app.py). Takes already-obtained credentials and
returns a list of Row objects — no I/O beyond the Gmail API itself.
"""

import os
import base64
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from collections import defaultdict

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from gmail_client import GmailClient, SUBSCRIPTION_QUERIES
from parser import extract_dates
from normalize import normalize_provider
from llm_extractor import classify_and_extract

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# If unset, scans the ENTIRE mailbox with no date cutoff. Set
# SEARCH_AFTER_DATE (format YYYY/MM/DD) as an env var to limit the scan
# to recent history instead — useful for large/old inboxes where a full
# scan would be slow or costly on API quota.
SEARCH_AFTER = os.environ.get("SEARCH_AFTER_DATE", "")

# Max messages fetched PER search query (there are 4 queries, so total
# possible messages fetched is up to 4x this, before dedup). Gmail API
# itself has no hard upper bound; this is a safety cap so a single scan
# can't run unboundedly long. Override via MAX_RESULTS_PER_QUERY if a
# very large mailbox needs a higher ceiling.
MAX_RESULTS_PER_QUERY = int(os.environ.get("MAX_RESULTS_PER_QUERY", "500"))


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
    matching email for that provider, not the most recent extracted
    date within the email body (which can be unreliable — e.g. a body
    mentioning a future renewal date).
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

    for msg_id, msg in client.get_messages_batch(all_msg_ids):
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        subject = headers.get("Subject", "")
        sender = headers.get("From", "")
        body = get_body_text(msg["payload"])

        result = classify_and_extract(subject, sender, body)
        if not result or not result.get("is_subscription"):
            continue

        # Skip low-confidence classifications rather than including
        # uncertain guesses in a client-facing report.
        if result.get("confidence", 1.0) < 0.6:
            continue

        dates = extract_dates(body)
        raw_provider = result.get("provider") or sender.split("<")[0].strip()
        provider = normalize_provider(raw_provider, sender)

        rows.append(Row(
            provider=provider,
            start_date=str(dates[0]) if dates else "",
            end_date=str(dates[-1]) if dates else "",
            amount=result.get("amount") or 0.0,
            currency=result.get("currency") or "",
            reason=result.get("reason") or "unknown",
            status=result.get("status") or "active",
            source_email_subject=subject,
            source_email_date=headers.get("Date", ""),
        ))

    return merge_to_current_subscriptions(rows)