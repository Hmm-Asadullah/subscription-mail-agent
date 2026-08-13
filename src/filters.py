"""
Lightweight local filtering to separate subscription-related emails
from plain newsletters, notifications, or unrelated mail, before the
more expensive regex parsing step runs.
"""

import re

# Strong phrases that almost only appear in actual billing/subscription emails.
# A match on any of these is enough on its own.
STRONG_PHRASES = {
    "payment confirmation", "payment successful", "payment receipt",
    "your invoice", "your receipt", "receipt for your payment",
    "auto-renew", "auto-renewal", "subscription renewed",
    "renewal confirmation", "your subscription has been",
    "trial ending", "trial ends", "trial period ends",
    "next billing date", "amount charged", "you have been charged",
    "cancellation confirmed", "successfully unsubscribed",
}

# Weaker/generic words that are only meaningful when paired with an
# actual price in the email — on their own they produce false positives
# (e.g. "plan" or "membership" showing up in LinkedIn/marketing copy).
WEAK_KEYWORDS = {
    "subscription", "renewal", "billing", "invoice", "membership", "plan",
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


def is_likely_subscription(subject: str, snippet: str, body_text: str) -> bool:
    text = f"{subject} {snippet} {body_text}".lower()

    if any(phrase in text for phrase in STRONG_PHRASES):
        return True

    has_price = bool(PRICE_HINT_RE.search(text))
    has_weak_keyword = any(kw in text for kw in WEAK_KEYWORDS)

    # Weak keywords only count as a subscription signal when a real price
    # is also present in the email — otherwise it's almost always a
    # notification, ad, or newsletter using the word incidentally.
    return has_price and has_weak_keyword