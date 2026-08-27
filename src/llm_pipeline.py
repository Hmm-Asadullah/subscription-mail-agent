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
from filters import is_likely_subscription, EXCLUDE_MARKERS, PRICE_HINT_RE, STRONG_PAID_PHRASES
from parser import extract_price, extract_dates, extract_status, extract_provider
from normalize import normalize_provider
from llm_extractor import classify_and_extract

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

import datetime

# Default: scan only the last 2 years to keep scans fast.
# Override by setting SEARCH_AFTER_DATE (format YYYY/MM/DD) as an env var,
# or by passing a date from the web UI.
_two_years_ago = (datetime.date.today() - datetime.timedelta(days=730)).strftime("%Y/%m/%d")
SEARCH_AFTER = os.environ.get("SEARCH_AFTER_DATE", _two_years_ago).strip()

# Max messages fetched PER search query (there are 4 queries, so total
# possible messages fetched is up to 4x this, before dedup).
# Default 50 keeps scan fast and below Gmail's per-user rate limit.
# With batch_size=10 in gmail_client.py, 50 messages = 5 batch calls with 0.3s pauses = ~2s fetch time.
# Override via env var: MAX_RESULTS_PER_QUERY=100 for a deeper scan.
MAX_RESULTS_PER_QUERY = int(os.environ.get("MAX_RESULTS_PER_QUERY", "50"))


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
    """
    Calculates how many months an active subscription has been running,
    from its earliest detected start date to today.
    Minimum is 1 month.
    """
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
    """
    Determines if a subscription is CURRENTLY active based on recency.
    Even if an old email was an active receipt/upgrade when sent, if no billing
    receipt has arrived within the expected recurrence window (e.g. within 65 days
    for monthly or 395 days for annual), the subscription has lapsed/expired.
    """
    from dateutil import parser as dateparser

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

        # Allow up to 395 days (~13 months) for annual plans, or 65 days (~2 months) for monthly
        max_allowed_days = 395 if is_annual else 65
        return days_ago <= max_allowed_days
    except Exception:
        return True


def merge_to_current_subscriptions(rows: list) -> list:
    """
    Collapses multiple emails from the same provider into a single row
    representing that subscription's CURRENT state.
    Strictly keeps ONLY currently ACTIVE subscriptions with a paid non-zero amount.
    Filters out lapsed/expired subscriptions that haven't billed recently.
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

        # Drop lapsed subscriptions where the last billing receipt is too old (e.g. from 2023)
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




def _process_single_message(item) -> Row | None:
    msg_id, msg = item
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("Subject", "")
    sender = headers.get("From", "")
    body = get_body_text(msg.get("payload", {}))
    snippet = msg.get("snippet", "")
    effective_body = body if body else snippet

    # Fast pre-filtering: Skip obvious cancellations, trials, newsletters, or marketing offers.
    # These cheap regex/string checks run before any LLM call so most emails
    # are rejected in microseconds.
    full_lower = f"{subject} {snippet} {effective_body}".lower()
    if any(marker in full_lower for marker in EXCLUDE_MARKERS):
        return None

    # Cheap price check — skip the LLM if there's no detectable price anywhere.
    # BYPASS: if a strong billing phrase is present (e.g. "subscription renewed",
    # "billed for your subscription"), always let the LLM decide — the amount
    # might be in an image, a table cell, or buried in HTML we couldn't parse.
    has_strong_phrase = any(phrase in full_lower for phrase in STRONG_PAID_PHRASES)
    if not has_strong_phrase and not PRICE_HINT_RE.search(full_lower):
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

    with ThreadPoolExecutor(max_workers=15) as executor:
        for row in executor.map(_process_single_message, fetched_messages):
            if row is not None:
                rows.append(row)

    return merge_to_current_subscriptions(rows)