"""
Regex and keyword-cue based extraction of subscription fields
from email subject + body text.
"""

import re
from dateutil import parser as dateparser

PRICE_RE = re.compile(r"(?P<currency>[$€£¥]|USD|EUR|GBP)\s?(?P<amount>\d+[.,]\d{2}|\d+)")

DATE_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s\d{1,2},?\s\d{4})\b",
    re.IGNORECASE,
)

FREQUENCY_CUES = {
    "monthly": ["/month", "per month", "monthly plan", "billed monthly"],
    "yearly": ["/year", "per year", "annual plan", "billed annually"],
    "weekly": ["/week", "per week", "weekly plan"],
}

STATUS_CUES = {
    "canceled": [
        "cancellation confirmed",
        "your subscription has been canceled",
        "your subscription has been cancelled",
        "successfully unsubscribed",
        "subscription expired", "has expired", "trial expired",
        "your subscription ended", "membership expired", "plan expired",
        "deactivated", "being deactivated", "deactivation",
        "account suspended", "terminated", "trial ends", "trial ending",
    ],
    "trial": ["free trial", "trial period", "trial ends", "trial ending"],
}

CURRENCY_SYMBOL_MAP = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}


def extract_price(text: str):
    m = PRICE_RE.search(text)
    if not m:
        return None, None
    amount = float(m.group("amount").replace(",", ""))
    currency = CURRENCY_SYMBOL_MAP.get(m.group("currency"), m.group("currency"))
    return amount, currency


def extract_dates(text: str):
    matches = DATE_RE.findall(text)
    parsed = []
    for m in matches:
        try:
            parsed.append(dateparser.parse(m, fuzzy=True).date())
        except (ValueError, OverflowError):
            continue
    return parsed


def extract_frequency(text: str) -> str:
    text_lower = text.lower()
    for freq, cues in FREQUENCY_CUES.items():
        if any(cue in text_lower for cue in cues):
            return freq
    return "monthly"


def extract_status(text: str) -> str:
    text_lower = text.lower()
    for status, cues in STATUS_CUES.items():
        if any(cue in text_lower for cue in cues):
            return status
    return "active"


def extract_provider(sender_email: str, sender_name: str) -> str:
    clean_name = sender_name.strip()
    if clean_name and clean_name.lower() not in ("no-reply", "noreply", "billing", "receipts", "invoices", "support", ""):
        if "@" not in clean_name:
            return clean_name
    domain = sender_email.split("@")[-1]
    domain_parts = domain.split(".")
    if len(domain_parts) >= 2:
        return domain_parts[-2].capitalize()
    return domain.capitalize()