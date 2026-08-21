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
FALLBACK_MODELS = [DEFAULT_MODEL, "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]

SYSTEM_PROMPT = """You are a strict and precise email classifier and data extractor for active paid recurring subscriptions.

Your goal is to ONLY identify emails confirming that the user has been BILLED or CHARGED for an ACTIVE RECURRING SUBSCRIPTION (monthly, yearly, or weekly).

CRITICAL DISTINCTION: SUBSCRIPTION BILLING vs. GENERIC PAYMENT EMAILS:
1. ONLY return "is_subscription": true when ALL of the following are true:
   - The email is a billing confirmation, receipt, or invoice from an automated subscription platform (SaaS software, cloud infrastructure, streaming, AI tool, hosting, domain, or recurring membership).
   - The charge is RECURRING — there is evidence of a monthly, yearly, or weekly billing cadence (e.g., "monthly plan", "billed every month", "annual subscription", "next billing date", "renewal date").
   - The amount charged is greater than zero.
   - The sender is the service provider itself — NOT a bank, payment gateway, or peer-to-peer payment app.

2. ALWAYS return "is_subscription": false for:
   - GENERIC PAYMENT CONFIRMATIONS: Emails from PayPal, Stripe, bank, Wise, Western Union, JazzCash, EasyPaisa, Payoneer, Razorpay, or any payment processor confirming a money transfer, withdrawal, payment sent/received, or top-up (e.g., "You sent $50", "Payment received from John", "Your transfer is complete", "Transaction confirmed").
   - BANK / WALLET NOTIFICATIONS: Account statements, bank alerts, credit card payment reminders, wallet top-ups, balance updates, or transaction alerts.
   - P2P / FREELANCE PAYMENTS: Payments sent or received between individuals (e.g., "Ahmed sent you $200", "Payment from client", "Upwork payment released").
   - PAYMENT REMINDERS / DUE NOTICES: Emails reminding you a payment is due or overdue, not confirming it was already charged.
   - CLIENT / FREELANCE / ONE-OFF INVOICES: Invoices for freelance work, consulting, custom projects, milestone payments, hourly labor, or services rendered.
   - ONE-TIME PURCHASES: Single retail orders, food delivery, hardware, electronics, travel/hotel/ticket bookings.
   - FREE TRIALS, TRIAL ENDINGS, CANCELLATION NOTICES, EXPIRATION NOTICES, DEACTIVATION EMAILS.
   - MARKETING EMAILS: Discount offers, upgrade prompts, promotional deals, newsletters (e.g., "50% off today only", "Deal ends tonight", "Use code SAVE20").
   - $0.00 / Free tier notifications.

3. KEY TEST — ask yourself:
   - "Did this email confirm a recurring subscription charge that will repeat automatically?" → true
   - "Is this a one-time payment, a bank transfer, a freelance payment, or just a payment notification?" → false

4. STATUS:
   - Use "active" ONLY for confirmed, currently-paid, ongoing subscriptions.
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
    truncated_body = body[:2500] if body else ""
    prompt_content = f"From: {sender}\nSubject: {subject}\n\nBody:\n{truncated_body}"

    # Try models in fallback order
    models_to_try = []
    for m in FALLBACK_MODELS:
        if m and m not in models_to_try:
            models_to_try.append(m)

    last_error = None
    for model_name in models_to_try:
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
            continue

    print(f"[llm_extractor] Failed to classify email ({ascii(subject)}): {last_error}")
    return None