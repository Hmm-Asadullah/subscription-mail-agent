"""
Core extraction pipeline, callable from either the CLI (main.py) or
the web app (web_app.py). Takes already-obtained credentials and
returns a list of Row objects — no I/O beyond the Gmail API itself.
"""

import os
import base64
from dataclasses import dataclass

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
        snippet = msg.get("snippet", "")

        if not is_likely_subscription(subject, snippet, body):
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

    return rows