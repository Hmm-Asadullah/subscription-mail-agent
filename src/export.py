"""
Exports subscription rows to CSV or JSON.
"""

import os
import pandas as pd
from dataclasses import asdict


def export_csv(rows: list, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df = pd.DataFrame([asdict(r) if hasattr(r, "__dataclass_fields__") else r for r in rows])
    df.to_csv(path, index=False)


def export_json(rows: list, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df = pd.DataFrame([asdict(r) if hasattr(r, "__dataclass_fields__") else r for r in rows])
    df.to_json(path, orient="records", indent=2, date_format="iso")