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
    # Generic payment / bank / P2P / wallet notifications:
    "you sent", "you received", "payment sent", "payment received from",
    "money sent", "money received", "transfer complete", "transfer successful",
    "transaction confirmed", "transaction alert", "transaction notification",
    "account statement", "bank statement", "wallet top-up", "balance update",
    "payment is due", "payment overdue", "due date reminder", "amount due",
    "your payment of", "payment reminder", "payment due",
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

# Comprehensive price-hint regex. Intentionally broad — false positives
# here just mean an extra LLM call; false negatives mean a missed subscription.
#
# Covers:
#   Currency symbols before number : $9.99  €10  £5  ¥1000  ₹499  ₩9900
#   ISO codes before/after number  : USD 9.99  9.99 USD  CAD 12.99  PKR 1499
#   Comma-formatted amounts        : $1,299.00  1.299,00 EUR
#   Whole-number amounts           : $10  €5
#   Period-suffixed amounts        : 9.99/mo  9.99/month  9.99/yr  9.99/year
#   Plain decimals in billing ctx  : 9.99 (matched only with /mo|/month suffix)
_CURRENCY_SYMBOLS = r"[$€£¥₹₩₪₪₺₿]"
_CURRENCY_CODES = (
    r"usd|eur|gbp|cad|aud|inr|pkr|mxn|brl|chf|sek|nok|dkk|"
    r"sgd|hkd|jpy|krw|czk|pln|huf|ron|try|aed|sar|qar|ngn|"
    r"zar|php|myr|idr|thb|vnd|cop|ars|pen|clp|egp|ils"
)
_AMOUNT = r"\d{1,3}(?:[,.\s]\d{3})*(?:[.,]\d{1,2})?"  # handles 1,299.00 / 1.299,00 / 1299

PRICE_HINT_RE = re.compile(
    r"(?:"
    # Symbol before amount: $9.99 / €1,299.00
    rf"{_CURRENCY_SYMBOLS}\s?{_AMOUNT}"
    # ISO code before amount: USD 9.99 / PKR 1,499
    rf"|(?:{_CURRENCY_CODES})\s?{_AMOUNT}"
    # Amount before ISO code: 9.99 USD / 12.99 EUR
    rf"|{_AMOUNT}\s?(?:{_CURRENCY_CODES})"
    # Amount with billing-period suffix (no symbol needed): 9.99/mo
    rf"|{_AMOUNT}\s?/\s?(?:mo|month|yr|year|week|wk)(?:\b|ly)"
    r")",
    re.IGNORECASE,
)


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