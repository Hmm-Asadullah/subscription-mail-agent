"""
CLI entry point — useful for your own testing/debugging.
Your client will use web_app.py instead; this file isn't for them.

Run from the project root: python src/main.py
"""

import os

from auth import get_credentials
from pipeline import run_pipeline
from export import export_csv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(BASE_DIR, "output", "subscriptions.csv")


def run():
    creds = get_credentials()
    print("Fetching and analyzing emails...")
    rows = run_pipeline(creds)
    export_csv(rows, OUTPUT_PATH)
    print(f"Extracted {len(rows)} candidate subscription rows.")
    print(f"Saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    run()