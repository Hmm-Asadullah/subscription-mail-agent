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

# Model configuration with fallback list in case of temporary provider spike/deprecation
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
FALLBACK_MODELS = [DEFAULT_MODEL, "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]

SYSTEM_PROMPT = """You are a strict and precise email classifier and data extractor for active paid recurring subscriptions.

Your goal is to ONLY identify emails confirming that the user has been BILLED or CHARGED for an ACTIVE RECURRING SUBSCRIPTION (monthly, yearly, or weekly).

CRITICAL DISTINCTION: RECURRING SUBSCRIPTION vs. ONE-TIME PURCHASES & INVOICES:
1. ONLY return "is_subscription": true when ALL of the following are true:
   - The email is an automated billing confirmation, renewal notice, or SaaS receipt for an ongoing RECURRING service (SaaS software, cloud compute/storage, AI tool, streaming service, domain/hosting with auto-renewal, or ongoing membership).
   - The charge is RECURRING — there is clear evidence of a continuous subscription cycle (e.g. "monthly plan", "billed monthly", "annual subscription", "next renewal date", "recurring charge", "billing cycle").
   - The amount charged is greater than zero.
   - The sender is the service provider itself — NOT a bank, payment processor notification, or P2P transfer.

2. ALWAYS return "is_subscription": false for:
   - ONE-TIME DIGITAL ASSETS & TEMPLATES: Purchases of website templates, Notion templates, Framer/Webflow templates, WordPress/Shopify themes, Figma UI kits, icon packs, font licenses, 3D models, graphics, digital downloads, e-books, online courses, tutorials (e.g., purchases from ThemeForest, Envato, Creative Market, Gumroad, UI8, Etsy, Lemon Squeezy, etc.).
   - ONE-TIME SOFTWARE LICENSES & LIFETIME DEALS: Lifetime access (LTD), single standard licenses, extended licenses, one-off plugin/app purchases, pay-once software.
   - FREELANCE / CONSULTING / CONTRACTOR INVOICES: Invoices for custom dev/design work, hourly labor, milestone payments, project retainers, consulting fees, services rendered (e.g., Upwork, Fiverr, contractor invoices, custom Stripe/PayPal invoices).
   - GENERIC RECEIPTS / INVOICES: An email having the word "Invoice", "Tax Invoice", "Receipt", or "Order Confirmation" by itself does NOT mean it is a subscription. If there is no recurring plan or renewal cadence, it is a one-time purchase.
   - PHYSICAL GOODS & RETAIL: Electronics, hardware, apparel, food delivery, airline/hotel tickets, ride-sharing.
   - GENERIC PAYMENT PROCESSOR NOTIFICATIONS: PayPal, Stripe, bank, Wise, JazzCash, EasyPaisa, Payoneer, Razorpay emails stating "You sent a payment", "Payment received", "Transfer completed".
   - BANK & WALLET ALERTS: Credit card statements, account balance alerts, wallet top-ups.
   - PAYMENT REMINDERS & DUE NOTICES: Invoices that are due or unpaid (not yet charged).
   - FREE TRIALS, EXPIRATIONS, CANCELLATION CONFIRMATIONS, DEACTIVATIONS.
   - MARKETING EMAILS & PROMOS: Upgrade deals, discount codes, newsletters.
   - $0.00 / Free tier notifications.

3. KEY TEST:
   - "Is this an automated recurring subscription that will charge again next month/year?" → true
   - "Is this a one-time template/theme purchase, digital download, freelance invoice, or one-off payment?" → false

4. STATUS:
   - Use "active" ONLY for confirmed, currently-paid, ongoing recurring subscriptions.
   - Anything else → "is_subscription": false.

Respond with ONLY a JSON object matching this exact schema:
{
  "is_subscription": true or false,
  "provider": "company/service name, or null",
  "amount": number greater than 0, or null,
  "currency": "3-letter code like USD, EUR, GBP, or null",
  "frequency": "monthly" or "yearly" or "weekly" or null,
  "status": "active" or null,
  "reason": "short category like software, streaming, cloud, or null",
  "confidence": number between 0 and 1
}"""


def _parse_json_response(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Attempt to extract JSON from surrounding text using brace matching
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def classify_and_extract(subject: str, sender: str, body: str) -> dict | None:
    """
    Sends one email to the LLM for combined classification + extraction.
    Tries primary model and falls back to alternate flash models if needed.
    Returns a normalized dict or None if LLM is unavailable.
    """
    # Billing info is usually in the first ~2000 chars of a receipt.
    truncated_body = body[:2000] if body else ""
    prompt_content = f"From: {sender}\nSubject: {subject}\n\nBody:\n{truncated_body}"

    # Try models in fallback order
    models_to_try = []
    for m in FALLBACK_MODELS:
        if m and m not in models_to_try:
            models_to_try.append(m)

    last_error = None
    for model_name in models_to_try:
        for attempt in range(2):  # Quick retry on rate limit
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt_content,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        max_output_tokens=1000,
                    ),
                )
                if response and response.text:
                    data = _parse_json_response(response.text)
                    if isinstance(data, dict):
                        # Sanitize status to lowercase
                        if data.get("status"):
                            data["status"] = str(data["status"]).strip().lower()
                        return data
            except Exception as e:
                last_error = e
                # If rate limited (429), brief pause before retry/fallback
                if "429" in str(e) or "ResourceExhausted" in str(e):
                    import time
                    time.sleep(1.0)
                    continue
                break

    print(f"[llm_extractor] Failed to classify email ({ascii(subject)}): {last_error}")
    return None