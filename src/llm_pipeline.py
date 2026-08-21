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
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from gmail_client import GmailClient, SUBSCRIPTION_QUERIES
from filters import is_likely_subscription, EXCLUDE_MARKERS
from parser import extract_price, extract_dates, extract_status, extract_provider
from normalize import normalize_provider
from llm_extractor import classify_and_extract

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# If unset, scans the ENTIRE mailbox with no date cutoff. Set
# SEARCH_AFTER_DATE (format YYYY/MM/DD) as an env var to limit the scan
# to recent history instead — useful for large/old inboxes where a full
# scan would be slow or costly on API quota.
SEARCH_AFTER = os.environ.get("SEARCH_AFTER_DATE", "").strip()

# Max messages fetched PER search query (there are 4 queries, so total
# possible messages fetched is up to 4x this, before dedup).
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

        merged.append(Row(
            provider=provider,
            start_date=earliest.start_date or earliest.source_email_date,
            end_date=latest.end_date or latest.source_email_date,
            amount=latest.amount,
            currency=latest.currency if latest.currency else "USD",
            reason=latest.reason if latest.reason else "subscription",
            status="active",
            source_email_subject=latest.source_email_subject,
            source_email_date=latest.source_email_date,
        ))

    return merged


def _process_single_message(item) -> Row | None:
    msg_id, msg = item
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("Subject", "")
    sender = headers.get("From", "")
    body = get_body_text(msg.get("payload", {}))
    snippet = msg.get("snippet", "")
    effective_body = body if body else snippet

    # Fast pre-filtering: Skip obvious cancellations, trials, newsletters, or marketing offers
    full_lower = f"{subject} {snippet} {effective_body}".lower()
    if any(marker in full_lower for marker in EXCLUDE_MARKERS):
        return None

    # Primary extraction using Gemini LLM
    result = classify_and_extract(subject, sender, effective_body)

    # If LLM succeeded and gave a verdict
    if result is not None:
        if not result.get("is_subscription"):
            return None
        if result.get("confidence", 1.0) < 0.6:
            return None

        status = str(result.get("status") or "").strip().lower()
        if status != "active":
            return None

        amount = float(result.get("amount") or 0.0)
        if amount <= 0.0:
            return None

        dates = extract_dates(effective_body)
        raw_provider = result.get("provider") or sender.split("<")[0].strip()
        provider = normalize_provider(raw_provider, sender)

        return Row(
            provider=provider,
            start_date=str(dates[0]) if dates else "",
            end_date=str(dates[-1]) if dates else "",
            amount=amount,
            currency=result.get("currency") or "USD",
            reason=result.get("reason") or "subscription",
            status="active",
            source_email_subject=subject,
            source_email_date=headers.get("Date", ""),
        )
    else:
        # Fallback heuristic parser when LLM is unavailable or offline
        if is_likely_subscription(subject, snippet, effective_body, sender):
            amount, currency = extract_price(f"{subject} {effective_body}")
            if not amount or amount <= 0.0:
                return None

            status = extract_status(f"{subject} {effective_body}")
            if status != "active":
                return None

            dates = extract_dates(effective_body)
            sender_name = sender.split("<")[0].strip()
            raw_provider = extract_provider(sender, sender_name)
            provider = normalize_provider(raw_provider, sender)

            return Row(
                provider=provider,
                start_date=str(dates[0]) if dates else "",
                end_date=str(dates[-1]) if dates else "",
                amount=float(amount),
                currency=currency or "USD",
                reason="subscription",
                status="active",
                source_email_subject=subject,
                source_email_date=headers.get("Date", ""),
            )

    return None


def run_pipeline(
    creds,
    search_after: str = SEARCH_AFTER,
    search_before: str = "",
    max_results: int = MAX_RESULTS_PER_QUERY,
) -> list:
    client = GmailClient(creds)
    rows = []
    seen_ids = set()

    search_after = (search_after or "").strip()
    search_before = (search_before or "").strip()

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

    print(f"[llm_pipeline] Found {len(all_msg_ids)} unique candidate messages. Fetching & analyzing...")

    fetched_messages = list(client.get_messages_batch(all_msg_ids))
    print(f"[llm_pipeline] Fetched {len(fetched_messages)} messages. Processing concurrently...")

    with ThreadPoolExecutor(max_workers=5) as executor:
        for row in executor.map(_process_single_message, fetched_messages):
            if row is not None:
                rows.append(row)

    return merge_to_current_subscriptions(rows)