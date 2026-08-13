"""
Quick demo UI. Run locally with:
    streamlit run src/streamlit_app.py

Uses the existing desktop OAuth flow (auth.py) — clicking "Run Scan" will
pop open a browser tab for Google login the first time, same as before.
This is meant for a live local demo, not production hosting.
"""

import os
import sys
import io

import streamlit as st
import pandas as pd
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import get_credentials
from pipeline import run_pipeline

st.set_page_config(page_title="Subscription Email Scanner", page_icon="📧", layout="centered")

st.title("📧 Subscription Email Scanner")
st.write("Scan a Gmail inbox for active subscriptions, receipts, and renewals.")

email = st.text_input("Gmail address to scan", placeholder="client@gmail.com")

if "rows" not in st.session_state:
    st.session_state.rows = None

run_clicked = st.button("Run Scan", type="primary", disabled=not email)

if run_clicked:
    with st.spinner("Connecting to Gmail and scanning inbox... a browser tab will open for login."):
        try:
            creds = get_credentials()
            rows = run_pipeline(creds)
            st.session_state.rows = rows
        except Exception as e:
            st.error(f"Something went wrong: {e}")
            st.session_state.rows = None

if st.session_state.rows is not None:
    rows = st.session_state.rows

    if not rows:
        st.info("No subscriptions found in this inbox.")
    else:
        df = pd.DataFrame([asdict(r) for r in rows])
        display_df = df.rename(columns={
            "provider": "Provider",
            "start_date": "First Seen",
            "end_date": "Last Seen",
            "amount": "Amount",
            "currency": "Currency",
            "status": "Status",
            "source_email_subject": "Email Subject",
            "source_email_date": "Email Date",
        })[["Provider", "Amount", "Currency", "Status", "First Seen", "Last Seen", "Email Subject"]]

        st.success(f"Found {len(rows)} subscription-related emails.")
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)

        st.download_button(
            label="Download CSV",
            data=csv_buffer.getvalue(),
            file_name="subscriptions.csv",
            mime="text/csv",
        )
