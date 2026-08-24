"""
AI-driven end-to-end formula generation.

Design principle (same as the rest of the app): the AI proposes, the app
disposes. The LLM is only allowed to choose from a candidate ingredient list
we hand it (so cost/availability data is always real), and every formula it
returns is re-validated and renormalized in plain Python before anything is
shown as authoritative - percentages are forced to sum to exactly 100%,
hallucinated ingredient names are stripped out with a visible warning, and
the deterministic property/compatibility/regulatory/cost engines still run
on top of whatever the AI proposed.
"""

import json
import re

from utils.groq_client import call_groq, GroqError

FORMULA_SYSTEM_PROMPT = """You are a senior cosmetic formulation chemist working inside an R&D tool.

You will be given:
1. A candidate ingredient list - the ONLY materials you are allowed to use. Each has an
   exact inci_name, category, function, cost_per_kg_usd, and source (In-House or Worldwide).
2. A product brief: category, sub-type, free-text description, target positioning
   (Premium / Mid-Range / Budget), and a material sourcing strategy.
3. A list of known ingredient-pair incompatibilities to avoid combining.

Your job: design ONE complete, practical, balanced cosmetic formula using ONLY ingredients
from the candidate list, referenced by their EXACT inci_name string (copy it exactly,
do not rename, abbreviate, or invent variants).

Formulation rules you must follow:
- Percentages across all ingredients should sum to 100 (the app will do final rounding).
- Group ingredients into logical manufacturing phases (e.g. "Water Phase (A)", "Oil Phase (B)",
  "Cool-Down Phase (C)") appropriate to the product type.
- If the formula contains water/aqueous ingredients, it MUST include a preservative from the
  candidate list. If no suitable preservative exists in the candidate list, do not invent one -
  instead explicitly say so in formulation_notes and flag it as a gap.
- If both a water phase and an oil/emollient phase are present, include a suitable emulsifier
  from the candidate list.
- Do not combine ingredients flagged as "high" or "medium" severity incompatible in the
  provided list. Low-severity pairs are acceptable if formulation adjustments are noted.
- Respect the requested positioning: Premium should showcase higher-performance/luxury
  ingredients from the candidate list when available and justified; Budget should prioritize
  cost-effective, functional ingredients while still being safe and stable; Mid-Range balances
  the two. Use the cost_per_kg_usd values given to make this judgment relative to what's
  actually available - do not assume prices not given to you.
- Respect the material sourcing strategy: if it restricts you to In-House or Worldwide only,
  use ONLY ingredients tagged with that source. If combined, prefer In-House ingredients for
  commodity/base roles when a suitable one exists, and reserve Worldwide ingredients for
  specialty/performance roles not covered in-house, explaining the logic briefly.
- Stay within any max_use_percent-style practical norms you know for actives (e.g. don't put
  10% of a potent active where 1-2% is standard) even if not explicitly given a limit.

Separately, regardless of the sourcing strategy restriction, you may suggest up to 4
"recommended_worldwide_upgrades" - specific worldwide-sourced ingredients (from the candidate
list's Worldwide-tagged entries, or general well-known cosmetic ingredients if none fit) that
would elevate this formula for its stated positioning if the brand were open to sourcing them.
These are advisory only and are NOT part of the costed formula itself.

Respond with ONLY valid JSON - no markdown code fences, no commentary before or after - matching
exactly this schema:
{
  "formula_name": "string",
  "product_summary": "2-3 sentence description of the resulting product",
  "positioning_rationale": "1-3 sentences on how ingredient choices reflect the requested positioning",
  "sourcing_rationale": "1-2 sentences on how the material sourcing strategy was applied",
  "phases": [
    {
      "phase_name": "string, e.g. 'A - Water Phase'",
      "ingredients": [
        {"inci_name": "string - exact match from candidate list", "percent": number, "role": "short phrase, e.g. 'primary emulsifier'"}
      ]
    }
  ],
  "formulation_notes": "manufacturing order / process tips / any flagged gaps (e.g. missing preservative)",
  "key_claims": ["short marketing-style claim strings, e.g. 'Lightweight, fast-absorbing'"],
  "recommended_worldwide_upgrades": [
    {"inci_name": "string", "reason": "why this elevates the formula for the requested positioning", "cost_per_kg_usd": number_or_null}
  ]
}
"""


class FormulaGenerationError(Exception):
    pass


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def build_candidate_context(working_df):
    """Trim the working ingredient dataframe down to only what the AI needs, to keep the prompt lean."""
    cols = ["inci_name", "category", "function", "cost_per_kg_usd", "source"]
    trimmed = working_df[[c for c in cols if c in working_df.columns]].copy()
    trimmed = trimmed.where(trimmed.notna(), None)
    return trimmed.to_dict("records")


def build_incompat_context(incompat_data):
    return incompat_data.get("pair_rules", [])


def build_user_prompt(product_category, product_subtype, description, positioning, source_strategy,
                       candidate_ingredients, incompat_rules, currency_code="USD"):
    payload = {
        "product_brief": {
            "category": product_category,
            "sub_type": product_subtype,
            "description": description,
            "positioning": positioning,
            "material_sourcing_strategy": source_strategy,
        },
        "note": f"All cost_per_kg values below are in {currency_code}. Only use them for relative "
                f"comparison between ingredients (e.g. 'this is pricier than that') - do not assume "
                f"any particular absolute price tier, since currency and market vary.",
        "candidate_ingredients": candidate_ingredients,
        "known_incompatibilities": incompat_rules,
    }
    return (
        "Design a formula for this product brief using ONLY the candidate ingredients. "
        "Respond with ONLY the JSON object described in your instructions.\n\n"
        + json.dumps(payload, indent=2)
    )


def build_refinement_prompt(previous_meta, previous_phases, product_category, product_subtype, description,
                             positioning, source_strategy, refinement_instruction, candidate_ingredients,
                             incompat_rules, currency_code="USD", prior_refinements=None):
    payload = {
        "original_product_brief": {
            "category": product_category,
            "sub_type": product_subtype,
            "description": description,
            "positioning": positioning,
            "material_sourcing_strategy": source_strategy,
        },
        "note": f"All cost_per_kg values below are in {currency_code}. Use them only for relative comparison.",
        "candidate_ingredients": candidate_ingredients,
        "known_incompatibilities": incompat_rules,
        "previous_formula": {
            "formula_name": previous_meta.get("formula_name"),
            "product_summary": previous_meta.get("product_summary"),
            "phases": previous_phases,
            "formulation_notes": previous_meta.get("formulation_notes"),
            "key_claims": previous_meta.get("key_claims"),
        },
        "prior_refinement_requests_already_applied": prior_refinements or [],
        "new_refinement_request": refinement_instruction,
    }
    return (
        "You previously designed the formula in \"previous_formula\" below for this product brief. "
        "The chemist has reviewed it and now wants a REVISED version. Apply the "
        "\"new_refinement_request\" while keeping everything else about the previous formula that "
        "still makes sense (don't change things the chemist didn't ask about, unless the requested "
        "change requires it - e.g. rebalancing percentages). Still use ONLY ingredients from the "
        "candidate list, still respect the original positioning/sourcing strategy unless the "
        "refinement request explicitly changes them, and still avoid the listed incompatibilities. "
        "Respond with ONLY the JSON object in the same schema as before (formula_name, product_summary, "
        "positioning_rationale, sourcing_rationale, phases, formulation_notes, key_claims, "
        "recommended_worldwide_upgrades) - no markdown fences, no commentary.\n\n"
        + json.dumps(payload, indent=2)
    )


def _call_and_parse_json(api_key, model, user_prompt, on_retry=None):
    """Shared retry-on-malformed-JSON logic used by both generate_formula and refine_formula."""
    last_error = None
    for attempt in range(2):
        try:
            prompt = user_prompt if attempt == 0 else (
                user_prompt + "\n\nIMPORTANT: Your previous response was not valid JSON. "
                "Respond with ONLY a single valid JSON object, no markdown fences, no extra text."
            )
            reply = call_groq(api_key, model, prompt, temperature=0.3, system_override=FORMULA_SYSTEM_PROMPT, on_retry=on_retry)
            cleaned = _strip_json_fences(reply)
            data = json.loads(cleaned)
            return data
        except GroqError:
            raise
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            continue

    raise FormulaGenerationError(f"The AI didn't return valid formula data after retrying. Last error: {last_error}")


def generate_formula(api_key, model, product_category, product_subtype, description, positioning,
                      source_strategy, candidate_ingredients, incompat_rules, currency_code="USD", on_retry=None):
    """Call Groq (with retry on malformed JSON, plus rate-limit/transient-error retry inside
    call_groq itself) and return the parsed raw dict for a brand-new formula."""
    user_prompt = build_user_prompt(product_category, product_subtype, description, positioning,
                                     source_strategy, candidate_ingredients, incompat_rules, currency_code)
    return _call_and_parse_json(api_key, model, user_prompt, on_retry=on_retry)


def refine_formula(api_key, model, previous_meta, previous_phases, product_category, product_subtype,
                    description, positioning, source_strategy, refinement_instruction, candidate_ingredients,
                    incompat_rules, currency_code="USD", prior_refinements=None, on_retry=None):
    """Ask the AI to revise a previously-generated formula based on a refinement instruction,
    keeping the prior formula as context so it doesn't start from scratch."""
    user_prompt = build_refinement_prompt(
        previous_meta, previous_phases, product_category, product_subtype, description, positioning,
        source_strategy, refinement_instruction, candidate_ingredients, incompat_rules, currency_code,
        prior_refinements,
    )
    return _call_and_parse_json(api_key, model, user_prompt, on_retry=on_retry)


def validate_and_normalize(raw_formula: dict, candidate_names: set, worldwide_names: set):
    """
    Flatten phases into a flat ingredient list, drop any hallucinated
    ingredient names not in the candidate set (warning shown), and rescale
    percentages to sum to exactly 100.00.

    Returns (flat_ingredients, phases_for_display, warnings, meta)
    where flat_ingredients is [{"inci_name","percent"}] ready for the rest
    of the app's engines, and phases_for_display preserves phase/role
    grouping with the same rescaled percentages for a nicer UI.
    """
    warnings = []
    flat = []
    phases_out = []

    phases = raw_formula.get("phases", [])
    if not phases:
        raise FormulaGenerationError("The AI response had no ingredient phases - try regenerating.")

    for phase in phases:
        phase_name = phase.get("phase_name", "Unnamed Phase")
        kept_ingredients = []
        for ing in phase.get("ingredients", []):
            name = str(ing.get("inci_name", "")).strip()
            try:
                pct = float(ing.get("percent", 0))
            except (TypeError, ValueError):
                pct = 0.0
            if not name or pct <= 0:
                continue
            if name not in candidate_names:
                warnings.append(f"AI referenced \"{name}\", which isn't in your candidate list - it was dropped.")
                continue
            role = ing.get("role", "")
            flat.append({"inci_name": name, "percent": pct})
            kept_ingredients.append({"inci_name": name, "percent": pct, "role": role})
        if kept_ingredients:
            phases_out.append({"phase_name": phase_name, "ingredients": kept_ingredients})

    if not flat:
        raise FormulaGenerationError("No valid ingredients survived validation - try regenerating or widening your candidate materials.")

    total = sum(i["percent"] for i in flat)
    if total <= 0:
        raise FormulaGenerationError("The AI's percentages summed to zero - try regenerating.")

    deviation = abs(total - 100)
    if deviation > 8:
        warnings.append(f"AI's raw percentages summed to {total:.1f}% (not 100%) - the app rescaled everything proportionally to fit exactly 100%.")

    scale = 100.0 / total
    for i in flat:
        i["percent"] = round(i["percent"] * scale, 2)
    for p in phases_out:
        for i in p["ingredients"]:
            i["percent"] = round(i["percent"] * scale, 2)

    # Fix rounding drift so it sums to exactly 100.00
    drift = round(100.0 - sum(i["percent"] for i in flat), 2)
    if abs(drift) >= 0.01 and flat:
        largest = max(flat, key=lambda i: i["percent"])
        largest["percent"] = round(largest["percent"] + drift, 2)
        # apply the same correction inside phases_out for display consistency
        for p in phases_out:
            for i in p["ingredients"]:
                if i["inci_name"] == largest["inci_name"]:
                    i["percent"] = largest["percent"]
                    break

    # Validate recommended worldwide upgrades against the real worldwide database
    upgrades = []
    for u in raw_formula.get("recommended_worldwide_upgrades", []) or []:
        name = str(u.get("inci_name", "")).strip()
        if name and name in worldwide_names:
            upgrades.append({
                "inci_name": name,
                "reason": u.get("reason", ""),
                "cost_per_kg_usd": u.get("cost_per_kg_usd"),
            })
        elif name:
            warnings.append(f"Suggested upgrade \"{name}\" isn't in the worldwide database, so it was omitted.")

    meta = {
        "formula_name": raw_formula.get("formula_name", "Untitled Formula"),
        "product_summary": raw_formula.get("product_summary", ""),
        "positioning_rationale": raw_formula.get("positioning_rationale", ""),
        "sourcing_rationale": raw_formula.get("sourcing_rationale", ""),
        "formulation_notes": raw_formula.get("formulation_notes", ""),
        "key_claims": raw_formula.get("key_claims", []) or [],
        "recommended_worldwide_upgrades": upgrades,
    }

    return flat, phases_out, warnings, meta
