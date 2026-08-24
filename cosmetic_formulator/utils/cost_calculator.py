"""Batch cost calculation and same-function ingredient substitution search."""

import pandas as pd


def calculate_cost(formula_df: pd.DataFrame, ingredients_df: pd.DataFrame, batch_size_kg: float):
    line_items = []
    total_cost = 0.0
    missing = []
    missing_cost = []

    for _, row in formula_df.iterrows():
        info = ingredients_df[ingredients_df["inci_name"] == row["inci_name"]]
        if info.empty:
            missing.append(row["inci_name"])
            continue
        info = info.iloc[0]
        pct = float(row["percent"])
        kg_used = batch_size_kg * (pct / 100.0)

        raw_cost = info.get("cost_per_kg_usd") if hasattr(info, "get") else info["cost_per_kg_usd"]
        if pd.isna(raw_cost) or str(raw_cost).strip() == "":
            missing_cost.append(row["inci_name"])
            line_items.append({
                "inci_name": row["inci_name"],
                "percent": pct,
                "kg_used": round(kg_used, 4),
                "cost_per_kg_usd": None,
                "line_cost_usd": None,
                "sustainability_score": info.get("sustainability_score") if hasattr(info, "get") else None,
            })
            continue

        cost_per_kg = float(raw_cost)
        line_cost = kg_used * cost_per_kg
        total_cost += line_cost
        sustain = info.get("sustainability_score") if hasattr(info, "get") else info["sustainability_score"]
        line_items.append({
            "inci_name": row["inci_name"],
            "percent": pct,
            "kg_used": round(kg_used, 4),
            "cost_per_kg_usd": cost_per_kg,
            "line_cost_usd": round(line_cost, 2),
            "sustainability_score": sustain,
        })

    return {
        "line_items": line_items,
        "total_cost_usd": round(total_cost, 2),
        "cost_per_kg_batch_usd": round(total_cost / batch_size_kg, 4) if batch_size_kg else 0,
        "missing_from_db": missing,
        "missing_cost": missing_cost,
    }


def calculate_unit_economics(cost_per_kg_batch_usd: float, unit_fill_g: float, packaging_cost_per_unit: float = 0.0,
                              overhead_percent: float = 0.0, markup_multiplier: float = None):
    """
    Convert a batch's per-kg cost into per-unit economics.

    unit_fill_g: how many grams/mL of formula go into one finished unit (jar, bottle, tube).
    packaging_cost_per_unit: optional flat packaging/component cost per unit.
    overhead_percent: optional % added on top of (formula + packaging) cost for labor/overhead.
    markup_multiplier: optional - if given, also shows what a suggested retail price would be
                        at that multiple of total unit cost (purely a calculator on the number
                        the user supplies, not a pricing recommendation).
    """
    formula_cost_per_unit = cost_per_kg_batch_usd * (unit_fill_g / 1000.0)
    subtotal = formula_cost_per_unit + packaging_cost_per_unit
    overhead_amount = subtotal * (overhead_percent / 100.0)
    total_unit_cost = subtotal + overhead_amount

    result = {
        "formula_cost_per_unit_usd": round(formula_cost_per_unit, 4),
        "packaging_cost_per_unit_usd": round(packaging_cost_per_unit, 4),
        "overhead_amount_per_unit_usd": round(overhead_amount, 4),
        "total_unit_cost_usd": round(total_unit_cost, 4),
    }
    if markup_multiplier and markup_multiplier > 0:
        result["suggested_price_at_multiplier_usd"] = round(total_unit_cost * markup_multiplier, 2)
    return result


def units_from_batch(batch_size_kg: float, unit_fill_g: float) -> float:
    if unit_fill_g <= 0:
        return 0
    return (batch_size_kg * 1000.0) / unit_fill_g


def batch_size_from_units(units_desired: int, unit_fill_g: float) -> float:
    return (units_desired * unit_fill_g) / 1000.0


def find_substitutes(ingredient_name: str, ingredients_df: pd.DataFrame, max_results: int = 5):
    """
    Find ingredients that serve the same specific formulation function
    (e.g. "Primary emulsifier", "Antimicrobial", "UV filter/pigment") and are
    cheaper and/or more sustainable than the given ingredient, as swap
    candidates.
    """
    info = ingredients_df[ingredients_df["inci_name"] == ingredient_name]
    if info.empty:
        return []
    info = info.iloc[0]
    # Match on the specific "function" field rather than the broader "category"
    # field. Category alone would lump very different actives together (e.g.
    # Retinol and Benzoyl Peroxide are both "Active" but do completely
    # different jobs) - function is specific enough to only surface
    # ingredients that actually serve the same formulation purpose.
    function = info["function"]
    current_cost = float(info["cost_per_kg_usd"])
    current_sustain = float(info["sustainability_score"])

    candidates = ingredients_df[
        (ingredients_df["function"] == function) &
        (ingredients_df["inci_name"] != ingredient_name)
    ].copy()

    if candidates.empty:
        return []

    candidates["cost_delta_usd_per_kg"] = current_cost - candidates["cost_per_kg_usd"].astype(float)
    candidates["sustainability_delta"] = candidates["sustainability_score"].astype(float) - current_sustain

    # Rank: prioritize ingredients that are both cheaper AND at least as sustainable,
    # then fall back to cheaper-only, then more-sustainable-only.
    candidates = candidates.sort_values(
        by=["cost_delta_usd_per_kg", "sustainability_delta"],
        ascending=[False, False],
    )

    results = []
    for _, row in candidates.head(max_results).iterrows():
        results.append({
            "inci_name": row["inci_name"],
            "cost_per_kg_usd": float(row["cost_per_kg_usd"]),
            "cost_delta_usd_per_kg": round(float(row["cost_delta_usd_per_kg"]), 2),
            "sustainability_score": float(row["sustainability_score"]),
            "sustainability_delta": round(float(row["sustainability_delta"]), 1),
        })
    return results
