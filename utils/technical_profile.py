"""
Deterministic technical product profiling.

Computes facts directly from the actual formula composition - active
ingredient list, UV filter loading, emulsion/product type, preservation
system - rather than asking the AI to invent numbers. This matters most for
regulated efficacy claims like SPF: sunscreen SPF is a tested, regulated
number (FDA OTC monograph / ISO 24444), not something that can be reliably
predicted from ingredient percentages alone. Rather than fabricate a
precise-looking fake SPF value, this module reports the real, verifiable
fact (total UV filter concentration) and maps it to a *qualitative*,
heavily-caveated directional tier based on commonly cited mineral-sunscreen
formulation ranges - explicitly not a substitute for actual SPF testing.
"""

UV_FILTER_FUNCTION_MARKERS = ("UV filter",)

# Rough, commonly-cited formulation ranges for combined mineral UV filter
# (Titanium Dioxide + Zinc Oxide) loading, for DIRECTIONAL context only.
# These are illustrative industry rules of thumb, not a validated SPF
# prediction model - actual SPF depends on filter grade/coating, particle
# size, film formation, photostability, and can only be established via
# standardized in-vitro/in-vivo testing.
UV_TIER_THRESHOLDS = [
    (10.0, "Below typical range for a labeled SPF product"),
    (15.0, "In the range commonly used for lower SPF (~SPF 15) mineral formulations"),
    (20.0, "In the range commonly used for mid-range SPF (~SPF 30) mineral formulations"),
    (25.0, "In the range commonly used for higher SPF (~SPF 50) mineral formulations"),
]

SPF_DISCLAIMER = (
    "This is NOT an SPF value and must never be used as one. SPF is a regulated, tested "
    "claim (FDA OTC sunscreen monograph in the US; ISO 24444 in vitro/in vivo testing "
    "elsewhere) - it depends on filter grade, particle size/coating, film formation, and "
    "photostability, none of which can be determined from a percentage alone. This is a "
    "directional formulation-range reference only; any SPF claim requires standardized lab "
    "testing before labeling or sale."
)


def _is_uv_filter(function_text) -> bool:
    if not isinstance(function_text, str):
        return False
    return any(marker.lower() in function_text.lower() for marker in UV_FILTER_FUNCTION_MARKERS)


def compute_technical_profile(formula_df, ingredients_df, product_category: str = "", product_subtype: str = ""):
    """
    Returns a dict of deterministic, formula-grounded technical facts:
      - active_ingredients: [{name, percent, function}]
      - uv_filter: None, or {total_percent, filters: [{name, percent}], tier_label, disclaimer}
      - emulsion_type: str
      - preservation: {preservatives: [{name, percent}], total_percent, has_antioxidant, has_chelator}
      - functional_ingredients: {emulsifiers: [...], thickeners: [...]}
    """
    active_ingredients = []
    uv_filters = []
    preservatives = []
    emulsifiers = []
    thickeners = []
    has_antioxidant = False
    has_chelator = False
    has_water = False
    has_oil = False

    for _, row in formula_df.iterrows():
        info = ingredients_df[ingredients_df["inci_name"] == row["inci_name"]]
        if info.empty:
            continue
        info = info.iloc[0]
        pct = float(row["percent"])
        category = info.get("category") if hasattr(info, "get") else info["category"]
        function = info.get("function") if hasattr(info, "get") else info["function"]
        name = row["inci_name"]

        category_str = str(category) if category is not None else ""
        function_str = str(function) if function is not None else ""

        if category_str == "Active":
            active_ingredients.append({"name": name, "percent": pct, "function": function_str})
            if _is_uv_filter(function_str):
                uv_filters.append({"name": name, "percent": pct})

        if "Preservative" in category_str:
            preservatives.append({"name": name, "percent": pct})
        if category_str == "Antioxidant":
            has_antioxidant = True
        if category_str == "Chelating Agent":
            has_chelator = True
        if "Emulsifier" in category_str:
            emulsifiers.append(name)
        if "Thickener" in category_str:
            thickeners.append(name)
        if name == "Aqua":
            has_water = True
        if category_str in ("Emollient", "Emollient/Thickener", "Emollient/Emulsifier"):
            has_oil = True

    uv_summary = None
    is_sunscreen_context = "sunscreen" in product_subtype.lower() or "spf" in product_subtype.lower()
    if uv_filters or is_sunscreen_context:
        total_pct = round(sum(f["percent"] for f in uv_filters), 2)
        if not uv_filters:
            tier_label = "No UV filters present in this formula"
        else:
            tier_label = UV_TIER_THRESHOLDS[0][1]
            for threshold, label in UV_TIER_THRESHOLDS:
                if total_pct >= threshold:
                    tier_label = label
        uv_summary = {
            "total_percent": total_pct,
            "filters": uv_filters,
            "tier_label": tier_label,
            "disclaimer": SPF_DISCLAIMER,
        }

    if has_water and has_oil and emulsifiers:
        emulsion_type = "Oil-in-water (O/W) emulsion"
    elif has_water and not has_oil:
        emulsion_type = "Water-based solution/gel (no oil phase)"
    elif has_oil and not has_water:
        emulsion_type = "Anhydrous (oil/wax-based, no water phase)"
    elif has_water and has_oil and not emulsifiers:
        emulsion_type = "Water + oil phases present but no emulsifier detected - likely unstable as formulated"
    else:
        emulsion_type = "Not classifiable from current ingredients"

    preservation = {
        "preservatives": preservatives,
        "total_percent": round(sum(p["percent"] for p in preservatives), 3),
        "has_antioxidant": has_antioxidant,
        "has_chelator": has_chelator,
    }

    return {
        "active_ingredients": active_ingredients,
        "uv_filter": uv_summary,
        "emulsion_type": emulsion_type,
        "preservation": preservation,
        "functional_ingredients": {"emulsifiers": emulsifiers, "thickeners": thickeners},
    }


def render_technical_description(profile: dict, product_category: str, product_subtype: str) -> str:
    """Compose a short, factual technical description from the deterministic
    profile - no AI call involved, so it's always grounded in the real
    formula and never at risk of inventing a number (e.g. a fake SPF value)."""
    sentences = []
    emulsion_lower = profile['emulsion_type'][0].lower() + profile['emulsion_type'][1:]
    product_label = product_subtype or product_category
    if product_label:
        sentences.append(f"This {emulsion_lower} is formulated as a {product_label}.")
    else:
        sentences.append(f"This formula is a {emulsion_lower}.")

    actives = profile["active_ingredients"]
    if actives:
        active_list = ", ".join(f"{a['name']} ({a['percent']:g}%)" for a in actives)
        sentences.append(f"It contains {len(actives)} active ingredient(s): {active_list}.")
    else:
        sentences.append("No dedicated active ingredients are present in this formula.")

    uv = profile["uv_filter"]
    if uv and uv["filters"]:
        filter_list = ", ".join(f"{f['name']} {f['percent']:g}%" for f in uv["filters"])
        sentences.append(
            f"Combined UV filter loading is {uv['total_percent']:g}% ({filter_list}) - {uv['tier_label']}."
        )
        sentences.append(f"⚠️ {uv['disclaimer']}")
    elif uv:
        sentences.append(f"{uv['tier_label']} - add mineral or organic UV filters before this can function as sun protection.")

    preservation = profile["preservation"]
    if preservation["preservatives"]:
        p_list = ", ".join(f"{p['name']} {p['percent']:g}%" for p in preservation["preservatives"])
        support = []
        if preservation["has_antioxidant"]:
            support.append("an antioxidant")
        if preservation["has_chelator"]:
            support.append("a chelating agent")
        support_note = f", supported by {' and '.join(support)}" if support else ""
        sentences.append(f"Preserved using {p_list} (combined {preservation['total_percent']:g}%){support_note}.")
    else:
        is_aqueous = "water" in profile["emulsion_type"].lower() or "emulsion" in profile["emulsion_type"].lower()
        if is_aqueous:
            sentences.append("⚠️ No preservative was detected, despite this formula containing water - a real microbial contamination risk.")
        else:
            sentences.append("No preservative was detected.")

    return " ".join(sentences)
