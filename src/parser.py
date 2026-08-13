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
        "successfully unsubscribed",
    ],
    "trial": ["free trial", "trial period", "trial ends"],
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
    return "unknown"


def extract_status(text: str) -> str:
    text_lower = text.lower()
    for status, cues in STATUS_CUES.items():
        if any(cue in text_lower for cue in cues):
            return status
    return "active"  # default assumption if a billing email exists and no cancel signal found


def extract_provider(sender_email: str, sender_name: str) -> str:
    if sender_name and sender_name.strip().lower() not in ("no-reply", "noreply", ""):
        return sender_name.strip()
    domain = sender_email.split("@")[-1].split(".")[0]
    return domain.capitalize()