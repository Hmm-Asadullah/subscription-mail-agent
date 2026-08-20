"""
LLM-based classification + extraction for candidate subscription emails.
Uses Google's Gemini API (google-genai SDK).

Replaces filters.is_likely_subscription() and most of parser.py's regex
extraction (provider, price, status, reason) for emails reaching this
step. Date extraction still uses parser.extract_dates() — dates are
cheap and reliable to parse with regex, no need to spend an LLM call on
that part.
"""

import os
import json

from google import genai
from google.genai import types
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY environment variable is not set. "
        "Get one from https://aistudio.google.com/apikey and set it in "
        "your .env file (local) or your host's environment variables (production)."
    )

client = genai.Client(api_key=API_KEY)

# Flash tier is the right choice here — cheap, fast, and this is a
# straightforward classification/extraction task, not complex reasoning.
# Override via GEMINI_MODEL if you want a different tier.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

SYSTEM_PROMPT = """You are a precise email classifier and data extractor for a subscription-tracking tool.

Given an email's sender, subject, and body, determine if it is a genuine subscription billing email — \
a receipt, invoice, renewal notice, trial notice, expiration notice, or cancellation confirmation for a \
RECURRING paid service (software, streaming, memberships that auto-renew, SaaS tools, etc.).

Do NOT classify as a subscription email:
- Newsletters, social media notifications, job alerts, marketing promotions
- One-time purchase receipts: restaurant/food orders, retail orders, one-off invoices
- Utility bill payments, government e-payment receipts, tax payments — even if they recur
  periodically, they are NOT subscriptions in the SaaS/membership sense
- Any email whose only "recurring" signal is a loyalty program, rewards program, or membership
  mention in a footer/promo, when the email itself is a one-time transaction receipt

Pay close attention to STATUS. If the email says a subscription/trial/membership has EXPIRED, \
ENDED, or was CANCELED, the status is "canceled" — NOT "active", even though the email is about \
a subscription. Only mark "active" if the email confirms an ongoing, currently-paying subscription \
(a renewal receipt, a successful recurring payment, or similar).

Respond with ONLY a JSON object matching this exact schema:
{
  "is_subscription": true or false,
  "provider": "the service/company name, or null if not a subscription",
  "amount": number or null,
  "currency": "3-letter code like USD, or null",
  "frequency": "monthly" or "yearly" or "weekly" or "unknown" or null,
  "status": "active" or "canceled" or "trial" or null,
  "reason": "short category like streaming, software, cloud storage, fitness, or null",
  "confidence": number between 0 and 1 (be honest — use below 0.6 if genuinely unsure)
}"""


def classify_and_extract(subject: str, sender: str, body: str) -> dict | None:
    """
    Sends one email to the LLM for combined classification + extraction.
    Returns a dict matching the schema above, or None if the call fails
    or the response can't be parsed — fails safe, treated as not a match
    rather than crashing the whole scan over one bad email.
    """
    # Truncate the body to keep cost/latency predictable per email.
    truncated_body = body[:2000]

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=f"From: {sender}\nSubject: {subject}\n\nBody:\n{truncated_body}",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                max_output_tokens=300,
            ),
        )
        raw_text = response.text.strip()
        raw_text = raw_text.replace("```json", "").replace("```", "").strip()
        return json.loads(raw_text)
    except Exception as e:
        print(f"[llm_extractor] Failed to classify/extract email ({subject!r}): {e}")
        return None