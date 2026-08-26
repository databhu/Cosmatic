"""
Deterministic, rule-based estimates for pH, viscosity/texture, and stability.

These are heuristics meant to give an R&D chemist a fast directional read
before bench work - NOT a replacement for measuring the actual batch with a
calibrated pH meter and a viscometer.
"""

import pandas as pd

from utils.safe_convert import safe_float


def estimate_ph(formula_df: pd.DataFrame, ingredients_df: pd.DataFrame):
    """
    Weighted-average of each ingredient's typical pH midpoint, weighted by
    its % concentration in the formula. Ingredients with no defined pH range
    (fragrance, UV filters, etc.) are excluded from the weighting, as are
    rows with a missing/invalid percent (e.g. a blank cell in a manually
    edited formula table).
    """
    weighted_sum = 0.0
    weight_total = 0.0
    contributors = []

    for _, row in formula_df.iterrows():
        info = ingredients_df[ingredients_df["inci_name"] == row["inci_name"]]
        if info.empty:
            continue
        info = info.iloc[0]
        ph_min = safe_float(info["typical_ph_min"])
        ph_max = safe_float(info["typical_ph_max"])
        if ph_min is None or ph_max is None:
            continue
        pct = safe_float(row.get("percent"))
        if pct is None:
            continue
        midpoint = (ph_min + ph_max) / 2
        weighted_sum += midpoint * pct
        weight_total += pct
        contributors.append((row["inci_name"], midpoint, pct))

    if weight_total == 0:
        return None, contributors

    estimated = round(weighted_sum / weight_total, 2)
    return estimated, contributors


def estimate_viscosity(formula_df: pd.DataFrame, ingredients_df: pd.DataFrame):
    """
    Qualitative viscosity/texture estimate based on total % of thickening /
    structuring ingredient categories present in the formula.
    """
    thickening_categories = {
        "Thickener": 3.0,
        "Emulsifier/Thickener": 2.0,
        "Emulsifier": 1.2,
        "Emollient/Thickener": 1.0,
        "Emollient/Emulsifier": 1.0,
    }

    score = 0.0
    water_pct = 0.0
    oil_pct = 0.0
    details = []

    for _, row in formula_df.iterrows():
        info = ingredients_df[ingredients_df["inci_name"] == row["inci_name"]]
        if info.empty:
            continue
        info = info.iloc[0]
        pct = safe_float(row.get("percent"))
        if pct is None:
            continue
        category = info["category"]

        if info["inci_name"] == "Aqua":
            water_pct += pct
        if category in ("Emollient", "Emollient/Thickener", "Emollient/Emulsifier"):
            oil_pct += pct

        multiplier = thickening_categories.get(category, 0.0)
        if multiplier:
            contribution = pct * multiplier
            score += contribution
            details.append((row["inci_name"], category, round(contribution, 2)))

    if score < 3:
        texture = "Thin / fluid (lotion-like, pourable)"
    elif score < 10:
        texture = "Medium (typical lotion viscosity)"
    elif score < 25:
        texture = "Thick (rich cream consistency)"
    else:
        texture = "Very thick / gel-paste (may need thinning agent for pumpability)"

    phase_note = None
    if oil_pct > water_pct and water_pct > 0:
        phase_note = "Oil phase exceeds water phase - check this is intended as a water-in-oil emulsion, otherwise an emulsifier system suited for W/O is needed."

    return {
        "score": round(score, 2),
        "texture_estimate": texture,
        "water_phase_percent": round(water_pct, 2),
        "oil_phase_percent": round(oil_pct, 2),
        "phase_note": phase_note,
        "contributing_ingredients": details,
    }


def estimate_stability(formula_df: pd.DataFrame, ingredients_df: pd.DataFrame, incompat_flags):
    """
    Rough stability score (0-100) based on presence of:
      + a preservative system
      + an antioxidant (for O/W emulsions with unsaturated oils)
      + a chelating agent (helps preservative efficacy & prevents discoloration)
      + an emulsifier if both oil and water phases are present
      - any high/medium severity incompatibilities found
    """
    categories_present = set()
    has_water = False
    has_oil = False

    for _, row in formula_df.iterrows():
        info = ingredients_df[ingredients_df["inci_name"] == row["inci_name"]]
        if info.empty:
            continue
        info = info.iloc[0]
        categories_present.add(info["category"])
        if info["inci_name"] == "Aqua":
            has_water = True
        if info["category"] in ("Emollient", "Emollient/Thickener", "Emollient/Emulsifier"):
            has_oil = True

    score = 50
    notes = []

    has_preservative = any("Preservative" in c for c in categories_present)
    has_emulsifier = any("Emulsifier" in c for c in categories_present)
    has_antioxidant = "Antioxidant" in categories_present
    has_chelator = "Chelating Agent" in categories_present

    if has_preservative:
        score += 20
    else:
        score -= 25
        notes.append("No preservative detected - risk of microbial contamination, especially if water is present.")

    if has_water and has_oil:
        if has_emulsifier:
            score += 15
        else:
            score -= 30
            notes.append("Both water and oil phases are present with no emulsifier - the formula will likely separate.")

    if has_antioxidant:
        score += 5
    elif has_oil:
        notes.append("No antioxidant detected - consider one (e.g., Tocopherol) to slow oxidative rancidity of oils.")

    if has_chelator:
        score += 5

    high_sev = sum(1 for f in incompat_flags if f["severity"] == "high")
    med_sev = sum(1 for f in incompat_flags if f["severity"] == "medium")
    score -= high_sev * 20
    score -= med_sev * 8
    if high_sev:
        notes.append(f"{high_sev} high-severity ingredient conflict(s) found - see Compatibility tab.")
    if med_sev:
        notes.append(f"{med_sev} medium-severity ingredient conflict(s) found - see Compatibility tab.")

    score = max(0, min(100, score))
    return score, notes
