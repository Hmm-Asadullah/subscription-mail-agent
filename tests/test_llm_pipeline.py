import os
import sys
import base64
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from llm_extractor import classify_and_extract
from llm_pipeline import get_body_text, merge_to_current_subscriptions, Row


class TestLLMPipeline(unittest.TestCase):
    def test_recursive_body_extraction(self):
        # Create a nested multipart payload structure
        plain_text = "Your subscription of $20/month for ChatGPT Plus has renewed on Feb 1, 2026."
        plain_b64 = base64.urlsafe_b64encode(plain_text.encode("utf-8")).decode("utf-8")

        html_text = "<p>Your subscription of $20/month for ChatGPT Plus has renewed on Feb 1, 2026.</p>"
        html_b64 = base64.urlsafe_b64encode(html_text.encode("utf-8")).decode("utf-8")

        nested_payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": plain_b64}},
                        {"mimeType": "text/html", "body": {"data": html_b64}},
                    ]
                }
            ]
        }

        extracted = get_body_text(nested_payload)
        self.assertIn("ChatGPT Plus", extracted)
        self.assertIn("$20", extracted)

    def test_merge_to_current_subscriptions(self):
        rows = [
            Row(
                provider="Netflix",
                start_date="2025-01-01",
                end_date="2025-01-01",
                amount=15.99,
                currency="USD",
                reason="streaming",
                status="active",
                source_email_subject="Netflix Receipt Jan 2025",
                source_email_date="Wed, 1 Jan 2025 10:00:00 +0000",
            ),
            Row(
                provider="Netflix",
                start_date="2026-01-01",
                end_date="2026-01-01",
                amount=19.99,
                currency="USD",
                reason="streaming",
                status="active",
                source_email_subject="Netflix Receipt Jan 2026",
                source_email_date="Thu, 1 Jan 2026 10:00:00 +0000",
            ),
            Row(
                provider="Gym Membership",
                start_date="2025-06-01",
                end_date="2025-12-01",
                amount=50.00,
                currency="USD",
                reason="fitness",
                status="canceled",
                source_email_subject="Gym Cancellation Confirmation",
                source_email_date="Mon, 1 Dec 2025 10:00:00 +0000",
            ),
        ]

        merged = merge_to_current_subscriptions(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].provider, "Netflix")
        self.assertEqual(merged[0].amount, 19.99)
        self.assertEqual(merged[0].start_date, "2025-01-01")

    def test_llm_classification_live(self):
        subject = "Your Spotify Premium renewal receipt"
        sender = "Spotify <no-reply@spotify.com>"
        body = "Thanks for subscribing! Your monthly Spotify Premium subscription of $11.99 was charged on Jan 10, 2026. Next billing date is Feb 10, 2026."

        res = classify_and_extract(subject, sender, body)
        self.assertIsNotNone(res)
        self.assertTrue(res.get("is_subscription"))
        self.assertEqual(res.get("status"), "active")
        self.assertAlmostEqual(float(res.get("amount") or 0), 11.99)

    def test_rejection_of_deactivation_and_trial_and_promo(self):
        # 1. Deactivation email
        res1 = classify_and_extract(
            "[Important Notice] Your Confluence subscription is being deactivated",
            "No-Reply@Pucit-Team.Atlassian.Net",
            "Your Confluence subscription has ended and will be deactivated on April 30."
        )
        self.assertIsNotNone(res1)
        self.assertFalse(res1.get("is_subscription"))

        # 2. Trial ending email
        res2 = classify_and_extract(
            "Asad - Your SpiraTeam Trial Ends Tomorrow!",
            "Inflectra Sales <sales@inflectra.com>",
            "Hi Asad, your 30-day free trial of SpiraTeam will expire tomorrow. Buy a license today."
        )
        self.assertIsNotNone(res2)
        self.assertFalse(res2.get("is_subscription"))

        # 3. Marketing email
        res3 = classify_and_extract(
            "More clarity, more confidence, less effort 💎",
            "Grammarly <info@send.grammarly.com>",
            "Unlock Premium for $72.00/year to get writing suggestions and clarity."
        )
        self.assertIsNotNone(res3)
        self.assertFalse(res3.get("is_subscription"))


if __name__ == "__main__":
    unittest.main()
