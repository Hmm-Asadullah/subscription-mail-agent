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
    months_active: int = 1


def get_body_text(payload) -> str:
    """
    Recursively traverses Gmail's MIME payload structure (multipart/mixed,
    multipart/related, multipart/alternative, text/html, text/plain) to extract
    the best available readable text content.
    """
    if not payload:
        return ""

    def _extract(part):
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data", "")

        if mime_type == "text/plain" and body_data:
            try:
                return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
            except Exception:
                pass

        if mime_type == "text/html" and body_data:
            try:
                html = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
                return BeautifulSoup(html, "html.parser").get_text(" ")
            except Exception:
                pass

        subparts = part.get("parts", [])
        extracted_plain = ""
        extracted_html = ""
        for sub in subparts:
            text = _extract(sub)
            if text:
                if sub.get("mimeType") == "text/plain":
                    extracted_plain = text
                elif not extracted_html:
                    extracted_html = text

        return extracted_plain or extracted_html or ""

    text = _extract(payload)
    return text.strip() if text else ""


def _parse_email_date(date_str: str):
    """Parses an email Date header into a sortable datetime. Falls back
    to the minimum possible date if parsing fails, so malformed dates
    sort first rather than crashing the sort."""
    try:
        return parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        import datetime
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def calculate_months_active(start_date_str: str, fallback_date_str: str = "") -> int:
    from dateutil import parser as dateparser

    dt = None
    for s in [start_date_str, fallback_date_str]:
        if not s:
            continue
        try:
            dt = dateparser.parse(str(s), fuzzy=True)
            if dt:
                break
        except Exception:
            continue

    if not dt:
        return 1

    try:
        import datetime
        start_d = dt.date() if hasattr(dt, "date") else dt
        today = datetime.date.today()
        if start_d > today:
            return 1
        months = (today.year - start_d.year) * 12 + (today.month - start_d.month)
        if today.day >= start_d.day:
            months_count = months + 1
        else:
            months_count = max(1, months)
        return max(1, months_count)
    except Exception:
        return 1


def is_subscription_currently_active(latest_row: Row) -> bool:
    from dateutil import parser as dateparser
    import datetime

    # 1. If next renewal / period end date is in the future, it is definitely active
    if latest_row.end_date:
        try:
            end_dt = dateparser.parse(str(latest_row.end_date), fuzzy=True)
            if end_dt:
                end_d = end_dt.date() if hasattr(end_dt, "date") else end_dt
                today = datetime.date.today()
                if end_d >= today:
                    return True
        except Exception:
            pass

    # 2. Check the most recent billing email date
    latest_dt = None
    for s in [latest_row.source_email_date, latest_row.end_date]:
        if not s:
            continue
        try:
            latest_dt = dateparser.parse(str(s), fuzzy=True)
            if latest_dt:
                break
        except Exception:
            continue

    if not latest_dt:
        return False

    try:
        latest_d = latest_dt.date() if hasattr(latest_dt, "date") else latest_dt
        today = datetime.date.today()
        days_ago = (today - latest_d).days

        if days_ago <= 0:
            return True

        reason_lower = (latest_row.reason or "").lower()
        subject_lower = (latest_row.source_email_subject or "").lower()
        is_annual = any(w in reason_lower or w in subject_lower for w in ["year", "annual", "/yr", "yearly"])

        max_allowed_days = 395 if is_annual else 65
        return days_ago <= max_allowed_days
    except Exception:
        return True


def merge_to_current_subscriptions(rows: list) -> list:
    """
    Collapses multiple emails from the same provider into a single row
    representing that subscription's CURRENT state.
    Strictly keeps ONLY currently ACTIVE subscriptions with a paid non-zero amount.
    """
    groups = defaultdict(list)
    for row in rows:
        groups[row.provider].append(row)

    merged = []
    for provider, group in groups.items():
        group.sort(key=lambda r: _parse_email_date(r.source_email_date))
        earliest = group[0]
        latest = group[-1]

        status = (latest.status or "").strip().lower()
        if status != "active":
            continue  # drop canceled, trial, deactivated, or expired subscriptions

        if latest.amount <= 0.0:
            continue  # drop non-paid, $0, or trial notices

        if not is_subscription_currently_active(latest):
            continue

        start_d = earliest.start_date or earliest.source_email_date
        months_running = calculate_months_active(start_d, earliest.source_email_date)

        merged.append(Row(
            provider=provider,
            start_date=start_d,
            end_date=latest.end_date or latest.source_email_date,
            amount=latest.amount,
            currency=latest.currency if latest.currency else "USD",
            reason=latest.reason if latest.reason else "subscription",
            status="active",
            source_email_subject=latest.source_email_subject,
            source_email_date=latest.source_email_date,
            months_active=months_running,
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
        if not amount or amount <= 0.0:
            continue

        status = extract_status(f"{subject} {body}")
        if status != "active":
            continue

        dates = extract_dates(body)
        sender_name = sender.split("<")[0].strip()
        raw_provider = extract_provider(sender, sender_name)
        provider = normalize_provider(raw_provider, sender)

        rows.append(Row(
            provider=provider,
            start_date=str(dates[0]) if dates else "",
            end_date=str(dates[-1]) if dates else "",
            amount=float(amount),
            currency=currency or "USD",
            reason="subscription",
            status="active",
            source_email_subject=subject,
            source_email_date=headers.get("Date", ""),
        ))

    return merge_to_current_subscriptions(rows)