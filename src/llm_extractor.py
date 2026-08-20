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
FALLBACK_MODELS = [DEFAULT_MODEL, "gemini-3.7-flash", "gemini-flash-latest"]

SYSTEM_PROMPT = """You are a strict and precise email classifier and data extractor for active paid recurring subscriptions.

Your goal is to ONLY identify emails confirming that the user has been BILLED or CHARGED for an ACTIVE RECURRING SUBSCRIPTION (monthly, yearly, or weekly).

STRICT RULES:
1. ONLY return "is_subscription": true if:
   - The email is an actual RECEIPT, INVOICE, or BILLING CONFIRMATION for an active, paid recurring service (e.g., SaaS tools, software, streaming, cloud services, recurring paid memberships).
   - The email confirms an actual non-zero payment or charge was billed (amount > 0).
   - The billing cadence is recurring (monthly, yearly, weekly).

2. ALWAYS return "is_subscription": false (and provider: null, amount: null, status: null) for ANY of the following:
   - Free trials, trial ending notices, trial expiration notices (e.g. "Your Free Trial", "Trial Ends Tomorrow").
   - Cancellation notices, deactivation notices, account suspensions, expiration notices (e.g. "subscription is being deactivated", "cancellation confirmed", "subscription expired").
   - Marketing emails, newsletters, product promotions, discount offers, tips (e.g. Grammarly tips/promotions, upgrade discounts, feature announcements, even if they mention prices).
   - One-time purchases, single retail orders, restaurant/food orders, one-off utility bills, single invoice payments.
   - $0.00 / Free tier notifications / zero-amount notices.

3. STATUS:
   - Must be "active" for ongoing, currently-paid subscriptions.
   - If the subscription is canceled, deactivated, expired, or a trial, "is_subscription" MUST be false.

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

    print(f"[llm_extractor] Failed to classify email ({subject!r}): {last_error}")
    return None