"""
Web app — per-browser session isolation.

Each visitor's Gmail connection (OAuth token) lives inside their OWN
signed session cookie, encrypted at rest. Scan results are cached
server-side keyed by a random per-session ID. Nothing is shared
globally between browsers anymore — one person connecting or running
a scan can never affect what another person sees, with no manual
"Disconnect" step required for safety (though the button still exists
as a convenience/logout action for the person's own session).

Scanning runs in a background thread so the Gunicorn worker is never
blocked. The frontend polls /status/<job_id> every 2 s and redirects
to /results/<job_id> once the scan completes.

Run locally for testing with: python src/web_app.py
Deploy with gunicorn behind HTTPS in production.
"""

import dataclasses
import json
import os
import secrets
import threading

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from werkzeug.middleware.proxy_fix import ProxyFix

from export import export_csv
from llm_pipeline import MAX_RESULTS_PER_QUERY, run_pipeline

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

# ---------------------------------------------------------------------------
# In-memory stores (single-process; see multi-worker note in comments)
# ---------------------------------------------------------------------------

# Server-side cache for scan results, keyed by each session's own random
# ID (stored in the cookie, not the data itself — scan results can be
# too large to fit in a cookie). NOTE: this is in-memory, per-process.
# Fine for a single gunicorn worker (the current Procfile default). If
# you ever scale to multiple workers, this cache needs to move to a
# shared store (e.g. Redis) since each worker would otherwise have its
# own separate cache.
SCAN_CACHE: dict = {}

# Background job store. Keys are job_id strings; values are dicts:
#   {
#     "status":    "running" | "done" | "error",
#     "rows":      list[dict] | None,   # Row dataclasses serialised as dicts
#     "error":     str | None,
#     "csv_path":  str | None,
#     "start_date": str,
#     "end_date":   str,
#   }
JOB_STORE: dict = {}
JOB_STORE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_scan_background(job_id: str, creds_json: str, start_date: str, end_date: str):
    """
    Runs the pipeline in a daemon thread. Credentials are passed as a JSON
    string so the thread never touches Flask's request/session context.
    Results (or errors) are written back to JOB_STORE under the job_id.
    """
    try:
        creds = Credentials.from_authorized_user_info(json.loads(creds_json), SCOPES)
        rows = run_pipeline(
            creds,
            search_after=start_date,
            search_before=end_date,
            max_results=MAX_RESULTS_PER_QUERY,
        )

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        csv_path = os.path.join(OUTPUT_DIR, f"{job_id}.csv")
        export_csv(rows, csv_path)

        # Serialise Row dataclasses to plain dicts so they survive across requests
        rows_dicts = [dataclasses.asdict(r) for r in rows]

        with JOB_STORE_LOCK:
            JOB_STORE[job_id].update(
                status="done",
                rows=rows_dicts,
                csv_path=csv_path,
            )
        print(f"[web_app] Job {job_id} completed — {len(rows_dicts)} subscriptions found.")
    except Exception as exc:
        print(f"[web_app] Job {job_id} failed: {exc}")
        with JOB_STORE_LOCK:
            JOB_STORE[job_id].update(
                status="error",
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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
    """
    Starts a background scan and immediately redirects to the polling page.
    The actual pipeline runs in a daemon thread — this request returns in
    milliseconds, so Gunicorn workers are never blocked or timed out.
    """
    creds = get_valid_credentials()
    if not creds:
        return redirect(url_for("index"))

    # HTML date inputs submit as YYYY-MM-DD; Gmail's search syntax expects
    # YYYY/MM/DD. Both fields are optional.
    raw_start = request.form.get("start_date", "").strip()
    raw_end = request.form.get("end_date", "").strip()
    start_date = raw_start.replace("-", "/") if raw_start else ""
    end_date = raw_end.replace("-", "/") if raw_end else ""

    job_id = secrets.token_hex(16)

    with JOB_STORE_LOCK:
        JOB_STORE[job_id] = {
            "status": "running",
            "rows": None,
            "error": None,
            "csv_path": None,
            "start_date": raw_start,
            "end_date": raw_end,
        }

    # Serialise creds to JSON so they are safe to pass into the thread
    creds_json = creds.to_json()

    t = threading.Thread(
        target=_run_scan_background,
        args=(job_id, creds_json, start_date, end_date),
        daemon=True,
    )
    t.start()

    return redirect(url_for("scan_progress", job_id=job_id))


@app.route("/progress/<job_id>")
def scan_progress(job_id):
    """Renders the loading / polling page while the scan is still running."""
    with JOB_STORE_LOCK:
        job = JOB_STORE.get(job_id)

    if job is None:
        return redirect(url_for("index"))

    # If scan already finished by the time the browser hits this URL, skip
    # the loading page and go straight to results / error.
    if job["status"] == "done":
        return redirect(url_for("scan_results", job_id=job_id))

    if job["status"] == "error":
        return render_template(
            "dashboard.html",
            connected=True,
            rows=None,
            error=f"Scan failed: {job['error']}",
            start_date=job.get("start_date", ""),
            end_date=job.get("end_date", ""),
        )

    return render_template("scanning.html", job_id=job_id)


@app.route("/status/<job_id>")
def scan_status(job_id):
    """JSON endpoint polled by the frontend to check scan progress."""
    with JOB_STORE_LOCK:
        job = JOB_STORE.get(job_id)

    if job is None:
        return jsonify({"status": "not_found"}), 404

    return jsonify({"status": job["status"], "error": job.get("error")})


@app.route("/results/<job_id>")
def scan_results(job_id):
    """Displays the completed scan results page."""
    with JOB_STORE_LOCK:
        job = JOB_STORE.get(job_id)

    if job is None or job["status"] != "done":
        return redirect(url_for("index"))

    # Remember which job CSV to serve for /download
    session["last_job_id"] = job_id

    return render_template(
        "dashboard.html",
        connected=True,
        rows=job["rows"],
        error=None,
        start_date=job.get("start_date", ""),
        end_date=job.get("end_date", ""),
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
    job_id = session.get("last_job_id")
    if not job_id:
        return redirect(url_for("index"))

    with JOB_STORE_LOCK:
        job = JOB_STORE.get(job_id)

    if not job or not job.get("csv_path") or not os.path.exists(job["csv_path"]):
        return redirect(url_for("index"))

    return send_file(job["csv_path"], as_attachment=True, download_name="subscriptions.csv")


if __name__ == "__main__":
    # For local testing only. In production, run behind gunicorn + HTTPS.
    app.run(debug=True, port=5000)