"""
Handles user-uploaded "in-house" material lists (Excel/CSV) and merges them
with the built-in "worldwide" ingredient database into one working schema
that the rest of the app (property estimator, compatibility checker,
regulatory checker, cost calculator, AI formula generator) can operate on
identically regardless of where a material came from.
"""

import io
import re
import pandas as pd

# The canonical schema every ingredient row is normalized into. Only
# inci_name and cost_per_kg_usd are truly required from the user; everything
# else is optional and left blank if not supplied.
STANDARD_COLUMNS = [
    "inci_name", "category", "function", "cost_per_kg_usd",
    "typical_ph_min", "typical_ph_max", "sustainability_score",
    "stock_available_kg", "notes",
]

# Accepted header aliases -> standard column name (case-insensitive, spaces/underscores ignored)
HEADER_ALIASES = {
    "materialname": "inci_name", "material": "inci_name", "inciname": "inci_name",
    "ingredient": "inci_name", "ingredientname": "inci_name", "name": "inci_name",
    "tradename": "inci_name",
    "category": "category", "type": "category",
    "function": "function", "role": "function", "functiondescription": "function",
    "cost": "cost_per_kg_usd", "costperkg": "cost_per_kg_usd", "costkg": "cost_per_kg_usd",
    "pricekg": "cost_per_kg_usd", "priceperkg": "cost_per_kg_usd", "costperkgusd": "cost_per_kg_usd",
    "unitcost": "cost_per_kg_usd",
    "phmin": "typical_ph_min", "typicalphmin": "typical_ph_min", "minph": "typical_ph_min",
    "phmax": "typical_ph_max", "typicalphmax": "typical_ph_max", "maxph": "typical_ph_max",
    "sustainability": "sustainability_score", "sustainabilityscore": "sustainability_score",
    "stock": "stock_available_kg", "stockkg": "stock_available_kg",
    "availablestock": "stock_available_kg", "stockavailablekg": "stock_available_kg",
    "notes": "notes", "remarks": "notes", "comment": "notes", "comments": "notes",
}


class InHouseParseError(Exception):
    pass


def _normalize_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h).strip().lower())


def _match_header(normalized_header: str) -> str | None:
    """Match a normalized header to a standard column. Tries an exact alias
    match first; falls back to a prefix match against longer, unambiguous
    alias keys only (e.g. "costperkgyourappcurrency" -> "costperkg" ->
    cost_per_kg_usd) so descriptive suffixes like "(your app currency)"
    don't break recognition. Short/generic keys (e.g. "cost", "name") are
    exact-match only to avoid false positives like "costume" matching "cost".
    """
    if normalized_header in HEADER_ALIASES:
        return HEADER_ALIASES[normalized_header]
    for key in sorted(HEADER_ALIASES.keys(), key=len, reverse=True):
        if len(key) >= 8 and normalized_header.startswith(key):
            return HEADER_ALIASES[key]
    return None


def parse_inhouse_upload(uploaded_file) -> tuple[pd.DataFrame, list[str]]:
    """
    Parse an uploaded Excel (.xlsx/.xls) or CSV file of in-house materials
    into the STANDARD_COLUMNS schema.

    Returns (standardized_df, warnings). Raises InHouseParseError on
    unrecoverable problems (unreadable file, no name/cost columns found).
    """
    warnings = []
    name = uploaded_file.name.lower()

    try:
        if name.endswith((".xlsx", ".xls")):
            raw = pd.read_excel(uploaded_file)
        elif name.endswith(".csv"):
            raw = pd.read_csv(uploaded_file)
        else:
            raise InHouseParseError("Unsupported file type - please upload .xlsx, .xls, or .csv")
    except InHouseParseError:
        raise
    except Exception as e:
        raise InHouseParseError(f"Could not read the file: {e}")

    if raw.empty:
        raise InHouseParseError("The uploaded file has no rows.")

    # Map headers to standard columns
    col_map = {}
    for col in raw.columns:
        key = _normalize_header(col)
        matched = _match_header(key)
        if matched:
            col_map[col] = matched

    if not col_map or "inci_name" not in col_map.values():
        raise InHouseParseError(
            "Couldn't find a material name column. Rename it to 'Material Name' "
            "(or 'Ingredient') and re-upload - or use the downloadable template."
        )

    renamed = raw.rename(columns=col_map)
    standardized = pd.DataFrame(columns=STANDARD_COLUMNS)
    for col in STANDARD_COLUMNS:
        if col in renamed.columns:
            standardized[col] = renamed[col]
        else:
            standardized[col] = pd.NA

    # Drop rows with no material name
    before = len(standardized)
    standardized = standardized[standardized["inci_name"].notna() & (standardized["inci_name"].astype(str).str.strip() != "")]
    dropped = before - len(standardized)
    if dropped:
        warnings.append(f"Skipped {dropped} row(s) with no material name.")

    if "cost_per_kg_usd" not in renamed.columns:
        warnings.append("No cost column found - costs will be blank until you fill them in below. Cost math won't work for these until added.")
        standardized["cost_per_kg_usd"] = pd.NA
    else:
        standardized["cost_per_kg_usd"] = pd.to_numeric(standardized["cost_per_kg_usd"], errors="coerce")
        n_bad_cost = standardized["cost_per_kg_usd"].isna().sum()
        if n_bad_cost:
            warnings.append(f"{n_bad_cost} row(s) have a missing or non-numeric cost - fix these in the table below before costing.")

    for numcol in ("typical_ph_min", "typical_ph_max", "sustainability_score", "stock_available_kg"):
        standardized[numcol] = pd.to_numeric(standardized[numcol], errors="coerce")

    standardized["inci_name"] = standardized["inci_name"].astype(str).str.strip()
    standardized = standardized.reset_index(drop=True)

    dupe_count = standardized["inci_name"].duplicated().sum()
    if dupe_count:
        warnings.append(f"{dupe_count} duplicate material name(s) found - the last entry for each will be used.")
        standardized = standardized.drop_duplicates(subset="inci_name", keep="last").reset_index(drop=True)

    return standardized, warnings


def empty_inhouse_df() -> pd.DataFrame:
    return pd.DataFrame(columns=STANDARD_COLUMNS)


def generate_template_bytes() -> bytes:
    """Build a downloadable .xlsx template with headers and example rows."""
    example = pd.DataFrame([
        {
            "Material Name": "Glycerin",
            "Category": "Humectant",
            "Function": "Moisturizing",
            "Cost per kg (your app currency)": 2.20,
            "Typical pH Min": 4.0,
            "Typical pH Max": 8.0,
            "Sustainability Score (1-10)": 9,
            "Stock Available (kg)": 250,
            "Notes": "Local supplier, 2-week lead time",
        },
        {
            "Material Name": "House Blend Emulsifier X1",
            "Category": "Emulsifier",
            "Function": "Primary emulsifier",
            "Cost per kg (your app currency)": 6.50,
            "Typical pH Min": 4.5,
            "Typical pH Max": 7.5,
            "Sustainability Score (1-10)": "",
            "Stock Available (kg)": 40,
            "Notes": "",
        },
    ])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        example.to_excel(writer, index=False, sheet_name="In-House Materials")
        workbook = writer.book
        sheet = writer.sheets["In-House Materials"]
        header_fmt = workbook.add_format({"bold": True, "bg_color": "#1F2A44", "font_color": "white", "border": 1})
        for col_idx, col_name in enumerate(example.columns):
            sheet.write(0, col_idx, col_name, header_fmt)
            sheet.set_column(col_idx, col_idx, max(16, len(col_name) + 2))
    buffer.seek(0)
    return buffer.getvalue()


def merge_inhouse_upload(existing_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
    """Non-destructively fold a freshly parsed upload into the existing
    in-house table: new rows are added, and rows with a matching material
    name are updated (last-in wins) rather than wiping out everything else
    the user had already entered or typed manually."""
    if existing_df is None or existing_df.empty:
        return new_df.reset_index(drop=True)
    if new_df is None or new_df.empty:
        return existing_df.reset_index(drop=True)
    combined = pd.concat([existing_df, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset="inci_name", keep="last").reset_index(drop=True)
    return combined


def add_manual_material(existing_df: pd.DataFrame, inci_name: str, category: str = "", function: str = "",
                         cost_per_kg: float = None, ph_min: float = None, ph_max: float = None,
                         sustainability_score: float = None, stock_kg: float = None, notes: str = "") -> pd.DataFrame:
    """Append (or update, if the name already exists) a single manually-entered material."""
    new_row = pd.DataFrame([{
        "inci_name": str(inci_name).strip(), "category": category, "function": function,
        "cost_per_kg_usd": cost_per_kg, "typical_ph_min": ph_min, "typical_ph_max": ph_max,
        "sustainability_score": sustainability_score, "stock_available_kg": stock_kg, "notes": notes,
    }])
    return merge_inhouse_upload(existing_df, new_row)


def merge_material_sources(worldwide_df: pd.DataFrame, inhouse_df: pd.DataFrame, source_strategy: str) -> pd.DataFrame:
    """
    Build the unified working ingredient dataframe based on the chosen
    source strategy: 'In-House', 'Worldwide', or 'In-House + Worldwide'.

    In-house rows are tagged source='In-House' and worldwide rows
    source='Worldwide'. If the same inci_name exists in both, the in-house
    entry (the user's real cost/stock) takes precedence.
    """
    ww = worldwide_df.copy()
    ww["source"] = "Worldwide"

    ih = inhouse_df.copy()
    if not ih.empty:
        ih["source"] = "In-House"
        # Fill any columns the worldwide df has that in-house doesn't, so concat is clean
        for col in ww.columns:
            if col not in ih.columns:
                ih[col] = pd.NA

    if source_strategy == "In-House":
        return ih.reset_index(drop=True) if not ih.empty else pd.DataFrame(columns=list(ww.columns))
    if source_strategy == "Worldwide":
        return ww.reset_index(drop=True)

    # Combined: in-house entries win on name collisions
    combined = pd.concat([ww, ih], ignore_index=True) if not ih.empty else ww.copy()
    combined = combined.drop_duplicates(subset="inci_name", keep="last").reset_index(drop=True)
    return combined
