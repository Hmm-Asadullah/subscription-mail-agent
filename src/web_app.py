"""
Web app for the client — no terminal, no code, just a browser.

Flow:
  1. Client visits the site, clicks "Connect Gmail"
  2. Redirected to Google's consent screen (web OAuth flow, not desktop)
  3. Google redirects back to /oauth2callback with an auth code
  4. Token is saved (encrypted) server-side
  5. Client clicks "Run scan" -> pipeline runs -> results shown in a table
  6. Client clicks "Download CSV" -> gets the file

Run locally for testing with: python src/web_app.py
Deploy with a real WSGI server (gunicorn) behind HTTPS in production —
see the deployment notes below the code.
"""

import os
import json

from dotenv import load_dotenv
from flask import Flask, redirect, request, session, render_template, send_file, url_for
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from cryptography.fernet import Fernet

from pipeline import run_pipeline
from export import export_csv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))
CLIENT_SECRET_PATH = os.path.join(BASE_DIR, "credentials", "client_secret_web.json")
TOKEN_STORE_PATH = os.path.join(BASE_DIR, "credentials", "client_token.enc")
OUTPUT_PATH = os.path.join(BASE_DIR, "output", "subscriptions.csv")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_client_config() -> dict:
    """
    Loads the OAuth client config either from the GOOGLE_CLIENT_SECRET_JSON
    environment variable (used in production, e.g. Railway, where secret
    files can't be committed to the repo) or from the local
    client_secret_web.json file (used for local development).
    """
    raw = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
    if raw:
        return json.loads(raw)

    if os.path.exists(CLIENT_SECRET_PATH):
        with open(CLIENT_SECRET_PATH) as f:
            return json.load(f)

    raise RuntimeError(
        "No OAuth client config found. Set GOOGLE_CLIENT_SECRET_JSON (production) "
        "or place client_secret_web.json in credentials/ (local dev)."
    )


CLIENT_CONFIG = get_client_config()

# Must exactly match an Authorized redirect URI configured in Google Cloud
# Console for this OAuth client. Use your real domain in production.
REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:5000/oauth2callback")

# oauthlib refuses plain HTTP by default, even for localhost. Google itself
# allows http://localhost as a testing exception, but the library doesn't
# know that unless told explicitly. Only enable this bypass when the
# redirect URI is actually localhost — never in production, where the
# redirect URI will be a real https:// address and this line has no effect.
if REDIRECT_URI.startswith("http://localhost"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# Secret key used to sign the Flask session cookie — set a real random
# value via environment variable in production, never hardcode it.
# dashboard.html lives directly in src/, alongside this file — not in a
# separate templates/ folder.
TEMPLATES_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

# Key used to encrypt the saved token at rest. Generate once with
# Fernet.generate_key() and store it as an environment variable —
# losing this key means the client has to reconnect their Gmail account.
FERNET_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY")
if not FERNET_KEY:
    raise RuntimeError(
        "TOKEN_ENCRYPTION_KEY environment variable is not set. "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )
fernet = Fernet(FERNET_KEY.encode())


def save_token(creds: Credentials):
    os.makedirs(os.path.dirname(TOKEN_STORE_PATH), exist_ok=True)
    encrypted = fernet.encrypt(creds.to_json().encode())
    with open(TOKEN_STORE_PATH, "wb") as f:
        f.write(encrypted)


def load_token():
    if not os.path.exists(TOKEN_STORE_PATH):
        return None
    with open(TOKEN_STORE_PATH, "rb") as f:
        encrypted = f.read()
    decrypted = fernet.decrypt(encrypted)
    return Credentials.from_authorized_user_info(json.loads(decrypted), SCOPES)


def get_valid_credentials():
    creds = load_token()
    if not creds:
        return None
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_token(creds)
        else:
            return None
    return creds


@app.route("/")
def index():
    creds = get_valid_credentials()
    connected = creds is not None
    return render_template("dashboard.html", connected=connected, rows=None)


@app.route("/connect")
def connect():
    flow = Flow.from_client_config(
        CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=True,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",       # needed to receive a refresh token
        include_granted_scopes="true",
        prompt="consent",            # forces refresh_token on every fresh connect
    )
    session["oauth_state"] = state
    session["code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/oauth2callback")
def oauth2callback():
    flow = Flow.from_client_config(
        CLIENT_CONFIG,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        state=session.get("oauth_state"),
        code_verifier=session.get("code_verifier"),
    )
    flow.fetch_token(authorization_response=request.url)
    save_token(flow.credentials)
    return redirect(url_for("index"))


@app.route("/run", methods=["POST"])
def run_scan():
    creds = get_valid_credentials()
    if not creds:
        return redirect(url_for("index"))

    rows = run_pipeline(creds)
    export_csv(rows, OUTPUT_PATH)

    return render_template("dashboard.html", connected=True, rows=rows)


@app.route("/download")
def download():
    if not os.path.exists(OUTPUT_PATH):
        return redirect(url_for("index"))
    return send_file(OUTPUT_PATH, as_attachment=True, download_name="subscriptions.csv")


if __name__ == "__main__":
    # For local testing only. In production, run behind gunicorn + HTTPS.
    app.run(debug=True, port=5000)