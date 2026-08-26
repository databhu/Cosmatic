"""
Regulatory compliance checking against the sample ingredients.csv database.

IMPORTANT: The allowed/max-percent figures shipped in data/ingredients.csv are
a small illustrative starting set (a handful of ingredients verified against
public sources at the time of writing, e.g. the EU's 2024/996 retinol limits).
This is NOT a complete or continuously updated regulatory database. Before
using this for real product submissions, replace/extend data/ingredients.csv
with a verified feed from the official sources:
  EU:    CosIng / Regulation (EC) No 1223/2009 and its annexes
  US:    FDA cosmetic ingredient rules / OTC drug monographs where relevant
  India: BIS cosmetic standards / CDSCO
"""

import pandas as pd

from utils.safe_convert import safe_float

REGIONS = {
    "EU": {"allowed_col": "eu_allowed", "max_col": "eu_max_percent", "notes_col": "eu_notes"},
    "US": {"allowed_col": "us_allowed", "max_col": "us_max_percent", "notes_col": "us_notes"},
    "India": {"allowed_col": "india_allowed", "max_col": "india_max_percent", "notes_col": "india_notes"},
}


def check_regulatory(formula_df, ingredients_df, region: str):
    cols = REGIONS[region]
    results = []

    for _, row in formula_df.iterrows():
        row_pct = safe_float(row.get("percent"))
        info = ingredients_df[ingredients_df["inci_name"] == row["inci_name"]]
        if info.empty:
            results.append({
                "inci_name": row["inci_name"],
                "percent": row_pct,
                "status": "unknown",
                "message": "Not in sample database - verify manually against official regulatory sources.",
            })
            continue

        info = info.iloc[0]
        allowed = info[cols["allowed_col"]]
        max_pct = info[cols["max_col"]]
        notes = info[cols["notes_col"]]
        if row_pct is None:
            results.append({
                "inci_name": row["inci_name"],
                "percent": None,
                "status": "unknown",
                "message": "No usable percentage on file for this row (blank or invalid) - can't assess against regional limits.",
            })
            continue
        pct = row_pct

        allowed_is_blank = pd.isna(allowed) or str(allowed).strip() == ""
        if allowed_is_blank:
            source_note = " (likely an in-house/custom material with no regulatory data on file)" if "source" in info.index and info.get("source") == "In-House" else ""
            results.append({
                "inci_name": row["inci_name"],
                "percent": pct,
                "status": "unknown",
                "message": f"No {region} regulatory data on file for this material{source_note} - verify manually.",
            })
            continue

        if allowed is False or str(allowed).strip().upper() == "FALSE":
            results.append({
                "inci_name": row["inci_name"],
                "percent": pct,
                "status": "banned",
                "message": notes if isinstance(notes, str) and notes else f"Not permitted in {region} cosmetics per sample data.",
            })
            continue

        if not pd.isna(max_pct) and max_pct not in (None, ""):
            try:
                max_pct_f = float(max_pct)
                if pct > max_pct_f:
                    results.append({
                        "inci_name": row["inci_name"],
                        "percent": pct,
                        "status": "over_limit",
                        "message": f"Exceeds {region} max of {max_pct_f}%. {notes if isinstance(notes, str) else ''}".strip(),
                    })
                    continue
            except (ValueError, TypeError):
                pass

        results.append({
            "inci_name": row["inci_name"],
            "percent": pct,
            "status": "ok",
            "message": notes if isinstance(notes, str) and notes else "No specific restriction in sample data.",
        })

    return results


def summarize(results):
    banned = [r for r in results if r["status"] == "banned"]
    over_limit = [r for r in results if r["status"] == "over_limit"]
    unknown = [r for r in results if r["status"] == "unknown"]
    if banned or over_limit:
        return "non_compliant"
    if unknown:
        return "needs_review"
    return "compliant"
