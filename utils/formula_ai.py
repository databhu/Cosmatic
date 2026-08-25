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

from utils.ai_client import AIError

FORMULA_SYSTEM_PROMPT_BASE = """You are a senior cosmetic formulation chemist working inside an R&D tool.

You will be given:
1. A candidate ingredient list{candidate_list_role_note}
2. A product brief: category, sub-type, free-text description, target positioning
   (Premium / Mid-Range / Budget), and a material sourcing strategy.
3. A list of known ingredient-pair incompatibilities to avoid combining.

Your job: design ONE complete, practical, balanced cosmetic formula{sourcing_directive}

If the product_brief includes a "benchmark_product_reference" (a real or well-known product
named as a texture/sensory/performance reference point), use it to calibrate formulation
choices - e.g. viscosity/thickener level, emollient richness, absorption speed, foam density,
matte vs. dewy finish - so the result is directionally comparable in feel and performance.
Do NOT copy or reproduce that product's actual formula, ingredient list, marketing claims,
brand name, or packaging/trade dress - use it only as a sensory/performance target, informed
by your general knowledge of what that type of product is typically like.

Formulation rules you must follow:
- Percentages across all ingredients should sum to 100 (the app will do final rounding).
  Add this up yourself before responding - getting close to exactly 100 on the first try
  means the app won't need to rescale anything, which keeps your intended ratios intact.
- Group ingredients into logical manufacturing phases (e.g. "Water Phase (A)", "Oil Phase (B)",
  "Cool-Down Phase (C)") appropriate to the product type.
- If the formula contains water/aqueous ingredients, it MUST include a real, commercially
  available preservative. If you cannot identify a suitable one, do not invent one -
  instead explicitly say so in formulation_notes and flag it as a gap.
- If both a water phase and an oil/emollient phase are present, include a suitable emulsifier.
- Do not combine ingredients flagged as "high" or "medium" severity incompatible in the
  provided list. Low-severity pairs are acceptable if formulation adjustments are noted.
- Respect the requested positioning: Premium should showcase higher-performance/luxury
  ingredients when available and justified; Budget should prioritize cost-effective,
  functional ingredients while still being safe and stable; Mid-Range balances the two.
- Stay within any max_use_percent-style practical norms you know for actives (e.g. don't put
  10% of a potent active where 1-2% is standard) even if not explicitly given a limit.
{ingredient_data_directive}
Separately, you may suggest up to 4 "recommended_worldwide_upgrades" - specific real,
commercially available ingredients (whether or not they're in the candidate list) that would
elevate this formula for its stated positioning if the brand were open to sourcing them. These
are advisory only and are NOT part of the costed formula itself.

Respond with ONLY valid JSON - no markdown code fences, no commentary before or after - matching
exactly this schema:
{{
  "formula_name": "string",
  "product_summary": "2-3 sentence description of the resulting product",
  "positioning_rationale": "1-3 sentences on how ingredient choices reflect the requested positioning",
  "sourcing_rationale": "1-2 sentences on how the material sourcing strategy was applied",
  "phases": [
    {{
      "phase_name": "string, e.g. 'A - Water Phase'",
      "ingredients": [
        {{
          "inci_name": "string{ingredient_name_note}",
          "percent": number,
          "role": "short phrase, e.g. 'primary emulsifier'"{ingredient_extra_fields}
        }}
      ]
    }}
  ],
  "formulation_notes": "manufacturing order / process tips / any flagged gaps (e.g. missing preservative)",
  "key_claims": ["short marketing-style claim strings, e.g. 'Lightweight, fast-absorbing'"],
  "recommended_worldwide_upgrades": [
    {{"inci_name": "string", "reason": "why this elevates the formula for the requested positioning", "cost_per_kg_usd": number_or_null}}
  ]
}}
"""

_RESTRICTED_FILL = {
    "candidate_list_role_note": " - the ONLY materials you are allowed to use. Each has an\n   exact inci_name, category, function, cost_per_kg_usd, and source (In-House or Worldwide).",
    "sourcing_directive": " using ONLY ingredients\nfrom the candidate list, referenced by their EXACT inci_name string (copy it exactly,\ndo not rename, abbreviate, or invent variants).",
    "ingredient_data_directive": (
        "- Respect the material sourcing strategy: if it restricts you to In-House only, use ONLY\n"
        "  ingredients tagged with that source.\n"
    ),
    "ingredient_name_note": " - exact match from candidate list",
    "ingredient_extra_fields": "",
}

_FREE_WORLDWIDE_FILL = {
    "candidate_list_role_note": " of materials the user already has cost/sourcing data for.\n   This is a STARTING POINT, not a restriction - see below.",
    "sourcing_directive": (
        ". The material sourcing strategy for this\nformula is Worldwide (or In-House + Worldwide): you have a FREE HAND to select any real,\ncommercially available cosmetic ingredient worldwide that best fits the brief - you are NOT\nlimited to the candidate list. Use the candidate list where it already has a good fit (their\ncost/data is already verified), and confidently name additional real ingredients by their\ncorrect INCI name where a better fit exists, informed by your knowledge of the current global\ncosmetic ingredient market. Never invent a plausible-sounding but fictitious ingredient name -\nonly name ingredients you are confident are real, correctly-named, commercially available\nmaterials."
    ),
    "ingredient_data_directive": (
        "- For any ingredient you select that is NOT in the candidate list, you MUST also provide\n"
        "  its category, function, and a realistic estimated_cost_per_kg_usd (in the SAME currency\n"
        "  as the candidate list costs above - your best current-market\n"
        "  estimate - clearly an estimate, not a locked quote) directly on that ingredient entry, so\n"
        "  the app can still cost and analyze it. Ingredients already in the candidate list don't need\n"
        "  these fields repeated - the app already has verified data for them.\n"
    ),
    "ingredient_name_note": " - exact real INCI name (from the candidate list, or any other real ingredient you select)",
    "ingredient_extra_fields": (
        ",\n          \"category\": \"string, only if this ingredient is NOT in the candidate list, e.g. Active, Emollient, Preservative, Humectant, Emulsifier, Thickener, Antioxidant\",\n"
        "          \"function\": \"string, only if NOT in the candidate list - specific function/role\",\n"
        "          \"estimated_cost_per_kg_usd\": "
        "\"number, only if NOT in the candidate list - your best current-market estimate, "
        "in the SAME currency as the candidate list costs above\""
    ),
}


def get_formula_system_prompt(free_worldwide: bool) -> str:
    """free_worldwide=True lifts the candidate-list restriction (used whenever
    the sourcing strategy includes Worldwide) and asks the AI to name real
    ingredients beyond the list, with its own cost/category estimate for
    anything not already in it. free_worldwide=False keeps the original
    strict behavior (used for In-House-only sourcing, where staying within
    the user's real material list is the whole point)."""
    fill = _FREE_WORLDWIDE_FILL if free_worldwide else _RESTRICTED_FILL
    return FORMULA_SYSTEM_PROMPT_BASE.format(**fill)


# Kept for backward compatibility with any external reference to the old
# always-restricted prompt constant.
FORMULA_SYSTEM_PROMPT = get_formula_system_prompt(free_worldwide=False)


class FormulaGenerationError(Exception):
    pass


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> str:
    """Real LLM output doesn't always perfectly follow 'respond with ONLY
    JSON' instructions - models sometimes add a preamble ("Here's the
    revised formula:"), a trailing note, or fence the JSON in the middle of
    other text. This tries several increasingly permissive strategies
    before giving up, since failing to parse a well-formed-but-wrapped
    response is the most common real-world cause of generation/refinement
    silently failing.
    """
    candidate = _strip_json_fences(text)

    # Strategy 1: does it parse as-is?
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        pass

    # Strategy 2: pull out a ```json ... ``` fenced block from anywhere in the text
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            json.loads(fence_match.group(1))
            return fence_match.group(1)
        except json.JSONDecodeError:
            pass

    # Strategy 3: find the first '{' and the LAST '}' and try that whole span -
    # handles a preamble/postamble wrapped around an otherwise-valid JSON object.
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        span = text[first_brace:last_brace + 1]
        try:
            json.loads(span)
            return span
        except json.JSONDecodeError:
            pass

    # Give up - return the original cleaned candidate so the caller's own
    # json.loads() raises the same error and can build a diagnostic message.
    return candidate


def build_candidate_context(working_df):
    """Trim the working ingredient dataframe down to only what the AI needs, to keep the prompt lean."""
    cols = ["inci_name", "category", "function", "cost_per_kg_usd", "source"]
    trimmed = working_df[[c for c in cols if c in working_df.columns]].copy()
    trimmed = trimmed.where(trimmed.notna(), None)
    return trimmed.to_dict("records")


def build_incompat_context(incompat_data):
    return incompat_data.get("pair_rules", [])


def build_user_prompt(product_category, product_subtype, description, positioning, source_strategy,
                       candidate_ingredients, incompat_rules, currency_code="USD", free_worldwide=False,
                       benchmark_product=None):
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
    if benchmark_product and benchmark_product.strip():
        payload["product_brief"]["benchmark_product_reference"] = benchmark_product.strip()
    if free_worldwide:
        lead = (
            "Design a formula for this product brief. You have a free hand to select any real, "
            "commercially available worldwide ingredient that best fits the brief, not just the "
            "candidate list below (see your instructions for details). "
        )
    else:
        lead = "Design a formula for this product brief using ONLY the candidate ingredients. "
    return lead + "Respond with ONLY the JSON object described in your instructions.\n\n" + json.dumps(payload, indent=2)


def build_refinement_prompt(previous_meta, previous_phases, product_category, product_subtype, description,
                             positioning, source_strategy, refinement_instruction, candidate_ingredients,
                             incompat_rules, currency_code="USD", prior_refinements=None, free_worldwide=False,
                             benchmark_product=None):
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
    if benchmark_product and benchmark_product.strip():
        payload["original_product_brief"]["benchmark_product_reference"] = benchmark_product.strip()
    if free_worldwide:
        sourcing_line = (
            "You still have a free hand to select any real, commercially available worldwide "
            "ingredient that best fits the brief and this refinement request, not just the "
            "candidate list (see your instructions). "
        )
    else:
        sourcing_line = "Still use ONLY ingredients from the candidate list. "
    return (
        "You previously designed the formula in \"previous_formula\" below for this product brief. "
        "The chemist has reviewed it and now wants a REVISED version. Apply the "
        "\"new_refinement_request\" while keeping everything else about the previous formula that "
        "still makes sense (don't change things the chemist didn't ask about, unless the requested "
        "change requires it - e.g. rebalancing percentages). " + sourcing_line +
        "Still respect the original positioning/sourcing strategy unless the "
        "refinement request explicitly changes them, and still avoid the listed incompatibilities. "
        "Respond with ONLY the JSON object in the same schema as before (formula_name, product_summary, "
        "positioning_rationale, sourcing_rationale, phases, formulation_notes, key_claims, "
        "recommended_worldwide_upgrades) - no markdown fences, no commentary.\n\n"
        + json.dumps(payload, indent=2)
    )


def _sum_phase_percentages(data: dict) -> float:
    """Lightweight sum of all ingredient percentages across phases, used by
    _call_and_parse_json's validity check - kept separate from the full
    validate_and_normalize logic since this only needs the total, not the
    flattened/registered ingredient data."""
    total = 0.0
    for phase in data.get("phases", []) or []:
        for ing in phase.get("ingredients", []) or []:
            try:
                total += float(ing.get("percent", 0))
            except (TypeError, ValueError):
                continue
    return total


def _call_and_parse_json(call_fn, user_prompt, system_prompt, on_retry=None):
    """Shared JSON-response validation used by both generate_formula and refine_formula.
    call_fn is a pre-configured callable (see ai_client.make_call_fn) that already
    knows which key(s)/model(s) to use and automatically advances through them -
    including on content that fails validation here, not just on network/rate-limit
    errors - so a model that can't produce a valid formula for this task gets
    automatically swapped out, not just blindly retried.

    Validity here means: (1) parses as JSON with at least one phase, and
    (2) the ingredient percentages sum reasonably close to 100 (within 8%).
    Checking (2) here - not just after the fact in validate_and_normalize -
    means a badly-off formula triggers an automatic retry/model-switch
    FIRST; forcing a rescale is only ever a last-resort fallback if every
    available model still can't produce a well-summed formula."""
    parsed_holder = {}

    def _validate(content):
        try:
            cleaned = _extract_json_object(content)
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            return False, str(e)

        # Store on every successful parse, even if the percentage check below
        # fails - so if EVERY model/key attempt ends up with a bad total, we
        # still have the last successfully-parsed attempt available as a
        # fallback (see the except block) rather than failing outright.
        parsed_holder["data"] = data

        total = _sum_phase_percentages(data)
        if abs(total - 100) > 8:
            return False, f"ingredient percentages summed to {total:.1f}%, not close to 100%"
        return True, None

    try:
        call_fn(user_prompt, temperature=0.3, system_override=system_prompt,
                on_retry=on_retry, validate_response=_validate)
    except AIError as e:
        if "data" in parsed_holder:
            # Every model's LAST attempt still had a bad percentage total (validate_response
            # rejected it every time) - fall back to whatever the final attempt produced
            # rather than failing outright; validate_and_normalize will rescale it with a
            # visible warning as the true last resort.
            return parsed_holder["data"]
        raise FormulaGenerationError(
            f"The AI couldn't produce a valid formula after trying all configured models/keys ({e}). "
            "This is usually transient - try clicking Generate/Refine again in a moment."
        )

    return parsed_holder["data"]


def generate_formula(call_fn, product_category, product_subtype, description, positioning,
                      source_strategy, candidate_ingredients, incompat_rules, currency_code="USD", on_retry=None,
                      benchmark_product=None):
    """Call the AI (with retry on malformed JSON, plus rate-limit/transient-error retry and
    cross-provider fallback inside call_fn itself) and return the parsed raw dict for a brand-new formula.

    When source_strategy includes Worldwide, the AI is given a free hand to
    name any real ingredient (not just the candidate list) - see
    get_formula_system_prompt. benchmark_product, if given, is a real or
    well-known reference product the AI should use to inform texture/
    sensory/performance targets - never to copy branding, formula, or claims."""
    free_worldwide = "Worldwide" in source_strategy
    system_prompt = get_formula_system_prompt(free_worldwide)
    user_prompt = build_user_prompt(product_category, product_subtype, description, positioning,
                                     source_strategy, candidate_ingredients, incompat_rules, currency_code,
                                     free_worldwide, benchmark_product)
    return _call_and_parse_json(call_fn, user_prompt, system_prompt, on_retry=on_retry)


def refine_formula(call_fn, previous_meta, previous_phases, product_category, product_subtype,
                    description, positioning, source_strategy, refinement_instruction, candidate_ingredients,
                    incompat_rules, currency_code="USD", prior_refinements=None, on_retry=None,
                    benchmark_product=None):
    """Ask the AI to revise a previously-generated formula based on a refinement instruction,
    keeping the prior formula as context so it doesn't start from scratch."""
    free_worldwide = "Worldwide" in source_strategy
    system_prompt = get_formula_system_prompt(free_worldwide)
    user_prompt = build_refinement_prompt(
        previous_meta, previous_phases, product_category, product_subtype, description, positioning,
        source_strategy, refinement_instruction, candidate_ingredients, incompat_rules, currency_code,
        prior_refinements, free_worldwide, benchmark_product,
    )
    return _call_and_parse_json(call_fn, user_prompt, system_prompt, on_retry=on_retry)


def validate_and_normalize(raw_formula: dict, candidate_names: set, worldwide_names: set, restrict_to_candidates: bool = True):
    """
    Flatten phases into a flat ingredient list and validate percentages sum
    close to 100.

    restrict_to_candidates=True (In-House-only sourcing): any ingredient not
    in candidate_names is dropped with a warning - this keeps the AI honest
    to the user's real, costed material list.

    restrict_to_candidates=False (Worldwide sourcing - the AI has a free
    hand): ingredient names are trusted as-is, no membership check, no
    "dropped" warnings. Any ingredient not already in candidate_names is
    collected into new_ingredients using the category/function/estimated
    cost the AI provided for it, so the app can still register, cost, and
    analyze it exactly like a known ingredient.

    Percentages: if they're already close to 100 (within 8%), only a small
    rounding-drift correction is applied - no user-visible rescale warning.
    Only a genuinely large deviation triggers rescaling, WITH a visible
    warning, since forcing a badly-off formula to fit changes the AI's
    intended ratios. (In practice a large deviation should be rare - see
    _call_and_parse_json, which treats it as an invalid response and
    automatically retries/switches models *before* this function is ever
    called with the bad data, so rescaling here is a last-resort safety net,
    not the primary correction mechanism.)

    Returns (flat_ingredients, phases_for_display, warnings, meta, new_ingredients)
    where new_ingredients is [{"inci_name","category","function","cost_per_kg_usd",...}]
    ready to be merged into the app's working ingredient table.
    """
    warnings = []
    flat = []
    phases_out = []
    new_ingredients = {}  # inci_name -> row dict, deduped

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

            is_known = name in candidate_names
            if not is_known:
                if restrict_to_candidates:
                    warnings.append(f"AI referenced \"{name}\", which isn't in your candidate list - it was dropped.")
                    continue
                # Free worldwide mode: trust the name, register it using
                # whatever category/function/cost the AI supplied.
                if name not in new_ingredients:
                    try:
                        cost = float(ing["estimated_cost_per_kg_usd"]) if ing.get("estimated_cost_per_kg_usd") not in (None, "") else None
                    except (TypeError, ValueError):
                        cost = None
                    new_ingredients[name] = {
                        "inci_name": name,
                        "category": ing.get("category", "") or "",
                        "function": ing.get("function", "") or "",
                        "cost_per_kg_usd": cost,
                        "typical_ph_min": None, "typical_ph_max": None,
                        "sustainability_score": None, "stock_available_kg": None,
                        "notes": "AI-selected worldwide ingredient (web/knowledge-sourced) - cost is an estimate, verify before sourcing.",
                    }

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
        warnings.append(f"AI's raw percentages summed to {total:.1f}% (not 100%) even after being asked to retry - the app rescaled everything proportionally to fit exactly 100%.")

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

    # Recommended worldwide upgrades: always allowed, whether or not they're
    # in the static worldwide database (per the same free-hand principle) -
    # registered the same way as any other off-list ingredient if needed.
    upgrades = []
    for u in raw_formula.get("recommended_worldwide_upgrades", []) or []:
        name = str(u.get("inci_name", "")).strip()
        if not name:
            continue
        upgrades.append({
            "inci_name": name,
            "reason": u.get("reason", ""),
            "cost_per_kg_usd": u.get("cost_per_kg_usd"),
        })
        if not restrict_to_candidates and name not in worldwide_names and name not in new_ingredients:
            try:
                cost = float(u["cost_per_kg_usd"]) if u.get("cost_per_kg_usd") not in (None, "") else None
            except (TypeError, ValueError):
                cost = None
            new_ingredients[name] = {
                "inci_name": name, "category": "", "function": "", "cost_per_kg_usd": cost,
                "typical_ph_min": None, "typical_ph_max": None, "sustainability_score": None,
                "stock_available_kg": None,
                "notes": "AI-recommended worldwide upgrade (web/knowledge-sourced) - cost is an estimate, verify before sourcing.",
            }

    meta = {
        "formula_name": raw_formula.get("formula_name", "Untitled Formula"),
        "product_summary": raw_formula.get("product_summary", ""),
        "positioning_rationale": raw_formula.get("positioning_rationale", ""),
        "sourcing_rationale": raw_formula.get("sourcing_rationale", ""),
        "formulation_notes": raw_formula.get("formulation_notes", ""),
        "key_claims": raw_formula.get("key_claims", []) or [],
        "recommended_worldwide_upgrades": upgrades,
    }

    return flat, phases_out, warnings, meta, list(new_ingredients.values())


# --------------------------------------------------------------------------
# Worldwide ingredient search (genuine live web search via Gemini's native
# Google Search grounding - see ai_client.call_gemini_search /
# search_gemini_with_fallback). Results are treated as AI-researched
# estimates (clearly labeled as such wherever displayed), not verified
# supplier quotes - same caveat posture as the rest of the app's
# cost/regulatory data. This is a best-effort enhancement layered on top of
# the free-hand Worldwide sourcing behavior in validate_and_normalize -
# search failing doesn't block formula generation, it just means the AI
# relies on its own knowledge instead of fresh search results for that run.
# --------------------------------------------------------------------------
WORLDWIDE_SEARCH_SYSTEM_PROMPT = """You are a cosmetic ingredient sourcing researcher with live web search access.

Given a product brief, use web search to find REAL, currently-available cosmetic
ingredients (by their correct INCI - International Nomenclature of Cosmetic
Ingredients - name) from worldwide suppliers that would suit this product. This is
open-ended research, not a short pick-list - search broadly and thoroughly across the
functional categories this product actually needs (e.g. actives, emollients,
emulsifiers, humectants, preservatives, thickeners, antioxidants - whichever apply to
the brief), the same way an experienced formulator would research sourcing options
before committing to a formula. Go beyond generic/well-known basics and surface
specific, accurate, currently-relevant options a formulator might not already have on
hand - specialty actives, notable extracts, or well-established functional
ingredients suited to the brief and its positioning.

Rules:
- Only include REAL ingredients with a real, correct INCI name you found via
  search - never invent an ingredient name or guess at spelling.
- Estimate a realistic current market cost per kg in USD for each, based on what
  you find - clearly an estimate, not a locked-in supplier quote.
- Avoid listing only extremely generic basics (Water, Glycerin, etc.) unless
  genuinely central to the brief - focus on ingredients that make this formula
  distinctive or well-suited to the stated positioning.
- There is no fixed quota - return as many well-suited, genuinely real ingredients
  as you can confidently identify across the relevant functional categories. Don't
  artificially limit yourself to a small handful if more genuinely fit, but don't
  pad the list with irrelevant or low-confidence entries either.

Respond with ONLY valid JSON - no markdown fences, no commentary - matching exactly:
{
  "web_sourced_ingredients": [
    {
      "inci_name": "string - exact, correct INCI name",
      "category": "string, e.g. Active, Emollient, Preservative, Humectant, Emulsifier, Thickener, Antioxidant",
      "function": "string - specific function/role",
      "estimated_cost_per_kg_usd": number,
      "sourcing_note": "string - brief note on why this fits and/or typical sourcing region",
      "source_confidence": "string - 'established' (widely used, well-documented) or 'emerging/niche'"
    }
  ]
}
"""


def search_worldwide_ingredients(api_keys, product_category, product_subtype, description, positioning, on_fallback=None):
    """
    Use Gemini's native Google Search grounding to find real, currently-
    relevant worldwide ingredients for the given brief. Raises
    AIError/FormulaGenerationError on failure - callers should treat this as
    a best-effort enhancement (show an info note, then continue with the
    static database) rather than blocking formula generation on it.

    Takes api_keys directly (not a call_fn) since search uses Gemini's
    native API - a different request/response shape than the OpenAI-
    compatible endpoint used for the main formula generation calls.
    """
    from utils.ai_client import search_gemini_with_fallback

    payload = {
        "product_brief": {
            "category": product_category, "sub_type": product_subtype,
            "description": description, "positioning": positioning,
        }
    }
    prompt = (
        "Search the web and find real, currently-available worldwide cosmetic ingredients suited "
        "to this product brief. Respond with ONLY the JSON described in your instructions.\n\n"
        + json.dumps(payload, indent=2)
    )
    reply = search_gemini_with_fallback(api_keys, prompt, system_override=WORLDWIDE_SEARCH_SYSTEM_PROMPT, on_fallback=on_fallback)
    cleaned = _extract_json_object(reply)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise FormulaGenerationError(f"Worldwide ingredient search returned unparseable results ({e}).")
    return data.get("web_sourced_ingredients", []) or []


def web_results_to_candidate_rows(web_ingredients, standard_columns):
    """Convert search results into rows matching the app's standard ingredient
    schema, tagged source='Worldwide (Web)'. Rows with no name or no numeric
    cost are dropped (silently - the caller can compare input/output length
    to report how many were usable, if desired)."""
    rows = []
    for item in web_ingredients or []:
        name = str(item.get("inci_name", "")).strip()
        if not name:
            continue
        try:
            cost = float(item.get("estimated_cost_per_kg_usd"))
        except (TypeError, ValueError):
            continue
        row = {col: None for col in standard_columns}
        row.update({
            "inci_name": name,
            "category": item.get("category", ""),
            "function": item.get("function", ""),
            "cost_per_kg_usd": cost,
            "notes": item.get("sourcing_note", ""),
        })
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Worldwide material alternative search (web-search-driven substitute finder)
# --------------------------------------------------------------------------
# Same native-API Google Search grounding as search_worldwide_ingredients
# above - used by the Cost & Sustainability tab's "Find alternatives"
# feature to search beyond the local database, not just match against the
# ~45-ingredient reference set. Best-effort: a failure here should be shown
# to the user (unlike the silent-fallback formula-generation search step),
# since the user explicitly asked for this search by clicking a button.
ALTERNATIVE_SEARCH_SYSTEM_PROMPT = """You are a cosmetic ingredient sourcing researcher with live web search access,
helping a formulation chemist find alternative/substitute ingredients for one specific
material already in their formula.

Given the target ingredient (its name, function/category, and the product's positioning
context), use web search to find REAL, currently-available alternative ingredients that
could substitute for it - similar function, but potentially cheaper, more sustainable,
easier to source, or otherwise advantageous for the stated positioning.

Rules:
- Only include REAL ingredients with a real, correct INCI name you found via search -
  never invent an ingredient name or guess at spelling.
- Each alternative must serve a genuinely similar function to the target ingredient -
  don't suggest something that wouldn't actually work as a substitute.
- Estimate a realistic current market cost per kg in USD for each, based on what you
  find - clearly an estimate, not a locked-in supplier quote.
- Briefly explain why each is a good alternative (cost, sustainability, availability,
  performance, or regulatory advantage).
- Return up to 8 alternatives, best-fit first.

Respond with ONLY valid JSON - no markdown fences, no commentary - matching exactly:
{
  "alternatives": [
    {
      "inci_name": "string - exact, correct INCI name",
      "category": "string, e.g. Active, Emollient, Preservative, Humectant, Emulsifier, Thickener, Antioxidant",
      "function": "string - specific function/role",
      "estimated_cost_per_kg_usd": number,
      "reason": "string - why this is a good alternative (cost/sustainability/availability/performance)",
      "source_confidence": "string - 'established' (widely used, well-documented) or 'emerging/niche'"
    }
  ]
}
"""


def search_alternative_materials(api_keys, target_name, target_function, target_category,
                                  positioning="", on_fallback=None):
    """
    Use Gemini's native Google Search grounding to find real alternative/
    substitute ingredients for a specific target material, going beyond the
    local database. Raises AIError/FormulaGenerationError on failure - the
    caller (a user-initiated button click) should surface this directly
    rather than silently swallowing it.
    """
    from utils.ai_client import search_gemini_with_fallback

    payload = {
        "target_ingredient": {
            "name": target_name, "function": target_function or "",
            "category": target_category or "", "positioning_context": positioning or "",
        }
    }
    prompt = (
        "Search the web and find real, currently-available alternative/substitute ingredients "
        "for this target ingredient. Respond with ONLY the JSON described in your instructions.\n\n"
        + json.dumps(payload, indent=2)
    )
    reply = search_gemini_with_fallback(api_keys, prompt, system_override=ALTERNATIVE_SEARCH_SYSTEM_PROMPT, on_fallback=on_fallback)
    cleaned = _extract_json_object(reply)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise FormulaGenerationError(f"Alternative material search returned unparseable results ({e}).")
    return data.get("alternatives", []) or []
