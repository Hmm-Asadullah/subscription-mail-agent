"""
Lightweight local filtering to separate subscription-related emails
from plain newsletters, notifications, one-time purchases, or unrelated
mail, before the more expensive regex parsing step runs.
"""

import re

from normalize import PROVIDER_ALIASES

# Strong phrases that almost only appear in actual RECURRING billing
# emails. A match on any of these is enough on its own — no price or
# other context needed. Deliberately excludes generic terms like "your
# receipt" or "payment receipt" — those appear on ANY transaction,
# subscription or not, and caused false positives on one-time purchases.
STRONG_PHRASES = {
    "auto-renew", "auto-renewal", "subscription renewed",
    "renewal confirmation", "your subscription has been",
    "subscription confirmed", "subscription activated",
    "trial ending", "trial ends", "trial period ends",
    "next billing date", "cancellation confirmed", "successfully unsubscribed",
    "billing cycle", "your bill is ready", "billing statement",
    "recurring payment confirmation", "subscription payment",
}

# Words that specifically signal RECURRING billing — meaningful on their
# own when paired with a price. Deliberately more specific than a bare
# "plan" or "membership" — those show up constantly in loyalty-program
# footers, meal plans, and marketing boilerplate on completely unrelated
# one-time receipts (restaurant orders, utility payments, etc.).
RECURRING_KEYWORDS = {
    "subscription", "renewal", "auto-renew", "auto renew",
    "recurring payment", "recurring billing", "billing cycle",
    "your plan renews", "membership renewal", "subscription plan",
    "monthly plan", "annual plan", "yearly plan",
}

# Gmail's automated-notification/social footer boilerplate — if this shows
# up with no price nearby, it's a notification email, not a billing one.
NEWSLETTER_MARKERS = {
    "unsubscribe from this newsletter",
    "weekly digest",
    "you're receiving this because you signed up for updates",
    "manage your email preferences",
}

PRICE_HINT_RE = re.compile(r"[$€£¥]\s?\d")


def is_likely_subscription(subject: str, snippet: str, body_text: str, sender: str = "") -> bool:
    text = f"{subject} {snippet} {body_text}".lower()
    sender_lower = sender.lower()

    # Known subscription providers (Netflix, Spotify, Adobe, etc.) get a
    # free pass on sender identity alone — we trust the domain regardless
    # of how that provider phrases their subject line.
    if any(domain in sender_lower for domain in PROVIDER_ALIASES):
        return True

    if any(phrase in text for phrase in STRONG_PHRASES):
        return True

    has_price = bool(PRICE_HINT_RE.search(text))
    has_recurring_keyword = any(kw in text for kw in RECURRING_KEYWORDS)

    # A recurring-specific keyword + price is a real subscription signal.
    # Generic words alone (invoice/receipt/payment) are deliberately NOT
    # sufficient even with a price — a one-time purchase has both too.
    return has_price and has_recurring_keyword