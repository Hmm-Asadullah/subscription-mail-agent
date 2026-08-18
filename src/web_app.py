"""
Web app — per-browser session isolation.

Each visitor's Gmail connection (OAuth token) lives inside their OWN
signed session cookie, encrypted at rest. Scan results are cached
server-side keyed by a random per-session ID. Nothing is shared
globally between browsers anymore — one person connecting or running
a scan can never affect what another person sees, with no manual
"Disconnect" step required for safety (though the button still exists
as a convenience/logout action for the person's own session).

Run locally for testing with: python src/web_app.py
Deploy with gunicorn behind HTTPS in production.
"""

import os
import json
import secrets

from dotenv import load_dotenv
from flask import Flask, redirect, request, session, render_template, send_file, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from cryptography.fernet import Fernet

from pipeline import run_pipeline
from export import export_csv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))
CLIENT_SECRET_PATH = os.path.join(BASE_DIR, "credentials", "client_secret_web.json")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

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

REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:5000/oauth2callback")

# oauthlib refuses plain HTTP by default, even for localhost. Google itself
# allows http://localhost as a testing exception, but the library doesn't
# know that unless told explicitly. Only enable this bypass when the
# redirect URI is actually localhost — never in production.
if REDIRECT_URI.startswith("http://localhost"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

TEMPLATES_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

# Railway (and most hosts) terminate HTTPS at their edge and forward
# requests to the container over plain HTTP internally. ProxyFix tells
# Flask to trust the X-Forwarded-Proto header so request.url correctly
# reports https://.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Key used to encrypt the OAuth token before it's stored in the session
# cookie. Generate once with Fernet.generate_key() and set as an env var.
FERNET_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY")
if not FERNET_KEY:
    raise RuntimeError(
        "TOKEN_ENCRYPTION_KEY environment variable is not set. "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )
fernet = Fernet(FERNET_KEY.encode())

# Server-side cache for scan results, keyed by each session's own random
# ID (stored in the cookie, not the data itself — scan results can be
# too large to fit in a cookie). NOTE: this is in-memory, per-process.
# Fine for a single gunicorn worker (the current Procfile default). If
# you ever scale to multiple workers, this cache needs to move to a
# shared store (e.g. Redis) since each worker would otherwise have its
# own separate cache.
SCAN_CACHE = {}


def get_session_id() -> str:
    if "sid" not in session:
        session["sid"] = secrets.token_hex(16)
    return session["sid"]


def save_token(creds: Credentials):
    """Encrypts the token and stores it in THIS visitor's own session cookie."""
    encrypted = fernet.encrypt(creds.to_json().encode()).decode()
    session["token"] = encrypted


def load_token():
    """Reads and decrypts the token from THIS visitor's own session cookie."""
    encrypted = session.get("token")
    if not encrypted:
        return None
    decrypted = fernet.decrypt(encrypted.encode())
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
    return render_template("dashboard.html", connected=connected, rows=None, start_date="", end_date="")


@app.route("/connect")
def connect():
    flow = Flow.from_client_config(
        CLIENT_CONFIG, scopes=SCOPES, redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=True,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
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

    # HTML date inputs submit as YYYY-MM-DD; Gmail's search syntax expects
    # YYYY/MM/DD. Both fields are optional.
    start_date = request.form.get("start_date", "").replace("-", "/")
    end_date = request.form.get("end_date", "").replace("-", "/")

    rows = run_pipeline(creds, search_after=start_date, search_before=end_date)

    sid = get_session_id()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, f"{sid}.csv")
    export_csv(rows, csv_path)
    SCAN_CACHE[sid] = {"rows": rows, "csv_path": csv_path}

    return render_template(
        "dashboard.html", connected=True, rows=rows,
        start_date=request.form.get("start_date", ""),
        end_date=request.form.get("end_date", ""),
    )


@app.route("/disconnect", methods=["POST"])
def disconnect():
    sid = session.get("sid")
    if sid and sid in SCAN_CACHE:
        csv_path = SCAN_CACHE[sid].get("csv_path")
        if csv_path and os.path.exists(csv_path):
            os.remove(csv_path)
        del SCAN_CACHE[sid]

    session.clear()
    return redirect(url_for("index"))


@app.route("/download")
def download():
    sid = session.get("sid")
    if not sid or sid not in SCAN_CACHE:
        return redirect(url_for("index"))

    csv_path = SCAN_CACHE[sid]["csv_path"]
    if not os.path.exists(csv_path):
        return redirect(url_for("index"))

    return send_file(csv_path, as_attachment=True, download_name="subscriptions.csv")


if __name__ == "__main__":
    # For local testing only. In production, run behind gunicorn + HTTPS.
    app.run(debug=True, port=5000)