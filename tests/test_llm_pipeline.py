import os
import sys
import base64
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from llm_extractor import classify_and_extract
from llm_pipeline import get_body_text, merge_to_current_subscriptions, Row
from filters import is_likely_subscription


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
        import datetime
        recent_date_str = datetime.date.today().strftime("%Y-%m-%d")
        recent_email_date = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

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
                start_date="2025-01-01",
                end_date=recent_date_str,
                amount=19.99,
                currency="USD",
                reason="streaming",
                status="active",
                source_email_subject="Netflix Receipt Current",
                source_email_date=recent_email_date,
            ),
            # Old lapsed subscription from 2023 (no cancellation email, but lapsed)
            Row(
                provider="Upwork",
                start_date="2020-11-13",
                end_date="2023-07-12",
                amount=20.00,
                currency="USD",
                reason="freelance platform",
                status="active",
                source_email_subject="Nice job upgrading your membership!",
                source_email_date="Wed, 12 Jul 2023 07:21:11 +0000",
            ),
            # Explicitly canceled subscription
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
        # Should only include active Netflix; Upwork (lapsed 2023) and Gym (canceled) dropped
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].provider, "Netflix")
        self.assertEqual(merged[0].amount, 19.99)
        self.assertEqual(merged[0].start_date, "2025-01-01")
        self.assertGreaterEqual(merged[0].months_active, 1)


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
            "More clarity, more confidence, less effort",
            "Grammarly <info@send.grammarly.com>",
            "Unlock Premium for $72.00/year to get writing suggestions and clarity."
        )
        self.assertIsNotNone(res3)
        self.assertFalse(res3.get("is_subscription"))

        # 4. LeetCode discount/deal email
        res4 = classify_and_extract(
            "Deal Ends Today - $40 off Annual Premium",
            "LeetCode <no-reply@leetcode.com>",
            "The $40 off on our Annual Premium Subscription is extended for one day! Use code THANKS2025 at checkout. Offer ends at 11:59pm."
        )
        self.assertIsNotNone(res4)
        self.assertFalse(res4.get("is_subscription"))

        # 5. Grammarly promo offer
        res5 = classify_and_extract(
            "Sound more confident, even under pressure",
            "Grammarly <info@send.grammarly.com>",
            "Writing under pressure? Don't let the clock run out on this deal. Pro is 50% off for a limited time only. [Get Pro]"
        )
        self.assertIsNotNone(res5)
        self.assertFalse(res5.get("is_subscription"))

    def test_rejection_of_client_and_freelance_invoices(self):
        # 1. Freelance milestone invoice
        res1 = classify_and_extract(
            "Invoice #1042 for Web App Development Milestone 2",
            "John Doe <john@freelanceagency.io>",
            "Hi Asad,\n\nPlease find attached Invoice #1042 for Milestone 2: Backend API Integration.\nTotal Due: $1,250.00.\nDue by March 15 via wire transfer.\n\nThank you,\nJohn"
        )
        self.assertIsNotNone(res1)
        self.assertFalse(res1.get("is_subscription"))

        # 2. Hourly consulting services invoice
        res2 = classify_and_extract(
            "Invoice for Consulting Services - July 2026",
            "Accounting <billing@clientpartner.com>",
            "Invoice #INV-2026-07\nServices Rendered: 25 hours consulting @ $80/hr\nTotal Amount: $2,000.00\nPayment terms: Net 30"
        )
        self.assertIsNotNone(res2)
        self.assertFalse(res2.get("is_subscription"))

    def test_acceptance_of_recurring_subscription_invoice(self):
        # Automated recurring subscription invoice for SaaS/Cloud platform
        res = classify_and_extract(
            "Your GitHub Pro monthly invoice is available",
            "GitHub Billing <billing@github.com>",
            "Your monthly subscription invoice for GitHub Pro is ready. We billed $4.00 to your card ending in 1234 for the billing period Mar 1 - Mar 31. Next payment date: Apr 1."
        )
        self.assertIsNotNone(res)
        self.assertTrue(res.get("is_subscription"))
        self.assertEqual(res.get("status"), "active")
        self.assertAlmostEqual(float(res.get("amount") or 0), 4.00)


    def test_rejection_of_one_time_template_and_digital_purchases(self):
        # 1. ThemeForest / Envato website template purchase
        res1 = classify_and_extract(
            "ThemeForest: Purchase Confirmation for Agency Next.js Template",
            "Envato Market <sales@envato.com>",
            "Thank you for purchasing Agency Next.js Website Template. Total: $29.00 USD. Single Standard License. Download your item from your downloads page."
        )
        self.assertIsNotNone(res1)
        self.assertFalse(res1.get("is_subscription"))

        # 2. Gumroad Framer UI kit / template
        res2 = classify_and_extract(
            "Receipt for Framer UI Kit & Components",
            "Gumroad <receipts@gumroad.com>",
            "You purchased Framer UI Kit by Designer for $49.00 USD. One-time payment. Lifetime access to updates. Access your content here."
        )
        self.assertIsNotNone(res2)
        self.assertFalse(res2.get("is_subscription"))

    def test_heuristic_filter_rejects_templates_and_promos(self):
        # Website template purchase
        self.assertFalse(is_likely_subscription(
            "ThemeForest: Purchase Confirmation for Agency Next.js Template",
            "Your website template purchase is complete",
            "Thank you for purchasing Agency Next.js Website Template. Total: $29.00 USD. Single Standard License.",
            "Envato Market <sales@envato.com>"
        ))

        # LeetCode $40 off deal
        self.assertFalse(is_likely_subscription(
            "🔔 Deal Ends Today - $40 off Annual Premium",
            "The $40 off on our Annual Premium Subscription is extended for one day!",
            "Use code THANKS2025 at checkout. What You Get with LeetCode Premium: Unlock questions. Invest yourself!",
            "LeetCode Team <no-reply@leetcode.com>"
        ))

        # Grammarly promo
        self.assertFalse(is_likely_subscription(
            "Sound more confident, even under pressure 💪",
            "Give your deadlines new lifelines",
            "Don't let the clock run out on this deal. Pro is 50% off for a limited time only. [Get Pro]",
            "Grammarly <info@send.grammarly.com>"
        ))



if __name__ == "__main__":
    unittest.main()

