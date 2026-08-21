"""
Lightweight local filtering to separate subscription-related emails
from plain newsletters, notifications, one-time purchases, or unrelated
mail, before the more expensive regex parsing step runs.
"""

import re

from normalize import PROVIDER_ALIASES

# Phrases that explicitly indicate the email is NOT an active paid billing receipt
EXCLUDE_MARKERS = {
    "deactivated", "being deactivated", "deactivation", "account suspended",
    "trial ends", "trial ending", "trial period ends", "free trial",
    "trial expired", "your trial", "trial confirmation",
    "cancellation confirmed", "successfully unsubscribed", "has been canceled",
    "has been cancelled", "subscription expired", "has expired",
    "subscription ended", "membership expired", "plan expired",
    "unsubscribe from this newsletter", "weekly digest",
    "manage your email preferences", "tips & tricks", "more clarity, more confidence",
    "milestone payment", "hours worked", "hourly rate", "project invoice",
    "consulting invoice", "freelance invoice", "statement of work",
    "services rendered", "scope of work", "contract work",
    # Promotional / discount / marketing offer markers:
    "deal ends", "deal ends today", "black friday", "cyber monday",
    "use code", "promo code", "coupon code", "discount", "special offer",
    "limited time offer", "limited time only", "get pro", "upgrade to pro",
    "upgrade now", "unlock premium", "try premium", "invest yourself",
    "off annual", "off premium", "% off", "off your next",
}

# Strong phrases that appear in actual RECURRING paid billing emails.
STRONG_PAID_PHRASES = {
    "subscription renewed", "renewal confirmation", "subscription payment received",
    "recurring payment confirmation", "subscription payment", "billed for your subscription",
    "payment for your subscription", "invoice for your subscription",
    "your subscription has renewed", "subscription invoice",
}

# Keywords indicating billing or subscription context
BILLING_KEYWORDS = {
    "subscription renewed", "renewal confirmation", "recurring payment",
    "recurring billing", "billing cycle", "your plan renews",
    "membership renewal", "payment confirmation", "payment received",
    "invoice", "receipt", "billed", "confirmación de pago", "rechnung",
}

PRICE_HINT_RE = re.compile(r"[$€£¥]\s?\d+(?:\.\d{2})?|\d+(?:\.\d{2})?\s?(?:usd|eur|gbp)", re.IGNORECASE)


def is_likely_subscription(subject: str, snippet: str, body_text: str, sender: str = "") -> bool:
    text = f"{subject} {snippet} {body_text}".lower()
    sender_lower = (sender or "").lower()

    # Drop immediately if negative/cancellation/trial/deactivation/promo phrases are found
    if any(marker in text for marker in EXCLUDE_MARKERS):
        return False

    # Paid active subscriptions MUST have a price
    has_price = bool(PRICE_HINT_RE.search(text))
    if not has_price:
        return False

    # Must contain strong paid phrase or billing keyword confirming a charge/renewal
    has_billing_kw = any(kw in text for kw in BILLING_KEYWORDS) or any(phrase in text for phrase in STRONG_PAID_PHRASES)
    if not has_billing_kw:
        return False

    # Known provider with verified billing context
    if any(domain in sender_lower or domain in text for domain in PROVIDER_ALIASES):
        return True

    return True