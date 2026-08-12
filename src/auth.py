import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = os.path.join(BASE_DIR, "credentials", "token.json")
CLIENT_SECRET_PATH = os.path.join(BASE_DIR, "credentials", "client_secret.json")


def get_credentials() -> Credentials:
    if not os.path.exists(CLIENT_SECRET_PATH):
        raise FileNotFoundError(
            f"client_secret.json not found at {CLIENT_SECRET_PATH}. "
            "Download it from Google Cloud Console > Credentials and place it there."
        )

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return creds


if __name__ == "__main__":
    get_credentials()
    print(f"Authentication successful. Token saved to {TOKEN_PATH}")