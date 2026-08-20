"""
Runs every .eml fixture in tests/fixtures/ through the actual
is_likely_subscription() and parser functions — no Gmail API calls,
no network access. Use this to sanity-check filter/parser changes
before running against your real inbox.

Run from the project root: python tests/test_fixtures.py
"""

import os
import sys
import email
from email import policy

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from filters import is_likely_subscription
from parser import extract_price, extract_dates, extract_status, extract_provider

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Expected outcome per fixture filename: True = should be flagged as subscription,
# False = should be filtered out (regression check for false positives).
EXPECTED = {
    "netflix_monthly_receipt.eml": True,
    "adobe_annual_invoice.eml": True,
    "spotify_cancellation.eml": True,
    "dropbox_trial_ending.eml": True,
    "netflix_non_english_receipt.eml": True,
    "forwarded_github_receipt.eml": True,
    "linkedin_notification_false_positive.eml": False,
    "plain_newsletter_false_positive.eml": False,
}


def load_eml(path):
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    subject = msg.get("Subject", "")
    sender = msg.get("From", "")
    body = msg.get_body(preferencelist=("plain",))
    body_text = body.get_content() if body else ""
    return subject, sender, body_text


def run():
    passed, failed = 0, 0

    for filename, expected in EXPECTED.items():
        path = os.path.join(FIXTURES_DIR, filename)
        if not os.path.exists(path):
            print(f"[MISSING] {filename}")
            failed += 1
            continue

        subject, sender, body = load_eml(path)
        result = is_likely_subscription(subject, "", body, sender=sender)

        status = "PASS" if result == expected else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1

        print(f"[{status}] {filename} — expected={expected}, got={result}")

        if result:
            amount, currency = extract_price(f"{subject} {body}")
            dates = extract_dates(body)
            sub_status = extract_status(f"{subject} {body}")
            provider = extract_provider(sender, sender.split("<")[0].strip())
            print(f"         provider={provider!r} amount={amount} currency={currency} "
                  f"status={sub_status} dates={dates}")

    print(f"\n{passed} passed, {failed} failed out of {len(EXPECTED)}")


if __name__ == "__main__":
    run()
