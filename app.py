import json
import os

import pandas as pd
import streamlit as st

from utils.groq_client import call_groq, GroqError, AVAILABLE_MODELS
from utils.property_estimator import estimate_ph, estimate_viscosity, estimate_stability
from utils.compatibility_checker import check_compatibility, sort_flags
from utils.regulatory_checker import check_regulatory, summarize, REGIONS
from utils.cost_calculator import calculate_cost, find_substitutes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

st.set_page_config(page_title="Cosmetic Formulation Assistant", page_icon="🧴", layout="wide")


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
@st.cache_data
def load_ingredients():
    df = pd.read_csv(os.path.join(BASE_DIR, "data", "ingredients.csv"))
    return df


@st.cache_data
def load_incompatibilities():
    with open(os.path.join(BASE_DIR, "data", "incompatibilities.json")) as f:
        return json.load(f)


ingredients_df = load_ingredients()
incompat_data = load_incompatibilities()

if "formula" not in st.session_state:
    # Seed with a simple starter O/W lotion so every tab has something to show.
    st.session_state.formula = [
        {"inci_name": "Aqua", "percent": 70.0},
        {"inci_name": "Glycerin", "percent": 5.0},
        {"inci_name": "Cetearyl Alcohol", "percent": 4.0},
        {"inci_name": "Glyceryl Stearate", "percent": 3.0},
        {"inci_name": "Squalane", "percent": 8.0},
        {"inci_name": "Niacinamide", "percent": 4.0},
        {"inci_name": "Phenoxyethanol", "percent": 0.9},
        {"inci_name": "Xanthan Gum", "percent": 0.3},
        {"inci_name": "Citric Acid", "percent": 0.2},
    ]


def formula_df():
    return pd.DataFrame(st.session_state.formula)


# --------------------------------------------------------------------------
# Sidebar - AI provider config
# --------------------------------------------------------------------------
def get_default_groq_key():
    """Read a GROQ_API_KEY from st.secrets if a secrets.toml is configured
    (e.g. on Streamlit Community Cloud), otherwise fall back to empty so the
    app still runs fine locally with no secrets file at all."""
    try:
        return st.secrets.get("GROQ_API_KEY", "")
    except Exception:
        return ""


with st.sidebar:
    st.header("⚙️ Settings")
    groq_api_key = st.text_input(
        "Groq API key",
        value=get_default_groq_key(),
        type="password",
        help="Get one at console.groq.com. Used only for AI narrative/suggestions - never for the regulatory or cost math.",
    )
    groq_model = st.selectbox("Groq model", AVAILABLE_MODELS, index=0)
    st.divider()
    region = st.selectbox("Regulatory region", list(REGIONS.keys()))
    batch_size_kg = st.number_input("Batch size (kg)", min_value=0.1, value=100.0, step=10.0)
    st.divider()
    st.caption(
        "⚠️ Sample regulatory & cost data are illustrative starting points for R&D "
        "exploration, not a certified compliance database. Verify against official "
        "EU CosIng, US FDA, and India BIS/CDSCO sources before any real submission."
    )

st.title("🧴 AI Cosmetic Formulation Assistant")
st.caption("Formula Predictor · Property Estimator · Regulatory Rule Checker · Cost & Sustainability Calculator")

tab_build, tab_compat, tab_props, tab_reg, tab_cost, tab_ai = st.tabs(
    ["1. Build Formula", "2. Compatibility", "3. Properties", "4. Regulatory", "5. Cost & Sustainability", "6. AI Assistant"]
)

# --------------------------------------------------------------------------
# TAB 1 - Build Formula
# --------------------------------------------------------------------------
with tab_build:
    st.subheader("Build your formula")
    col1, col2 = st.columns([2, 1])

    with col1:
        all_names = sorted(ingredients_df["inci_name"].tolist())
        new_ingredient = st.selectbox("Add an ingredient", all_names, key="add_ingredient_select")
        new_pct = st.number_input("Percent (%)", min_value=0.0, max_value=100.0, value=1.0, step=0.1, key="add_ingredient_pct")
        if st.button("➕ Add to formula"):
            existing = [row for row in st.session_state.formula if row["inci_name"] == new_ingredient]
            if existing:
                existing[0]["percent"] = new_pct
            else:
                st.session_state.formula.append({"inci_name": new_ingredient, "percent": new_pct})
            st.rerun()

    with col2:
        st.metric("Ingredients", len(st.session_state.formula))
        total_pct = sum(r["percent"] for r in st.session_state.formula)
        st.metric("Total %", round(total_pct, 2), delta=round(total_pct - 100, 2))
        if st.button("🗑️ Clear formula"):
            st.session_state.formula = []
            st.rerun()

    st.divider()
    if st.session_state.formula:
        edited = st.data_editor(
            formula_df(),
            num_rows="dynamic",
            column_config={
                "inci_name": st.column_config.SelectboxColumn("Ingredient (INCI)", options=all_names, required=True),
                "percent": st.column_config.NumberColumn("Percent (%)", min_value=0.0, max_value=100.0, step=0.1, required=True),
            },
            key="formula_editor",
            use_container_width=True,
        )
        st.session_state.formula = edited.to_dict("records")

        total_pct = sum(r["percent"] for r in st.session_state.formula)
        if abs(total_pct - 100) > 0.05:
            st.warning(f"Total is {total_pct:.2f}% — adjust so the formula sums to 100% before relying on the estimates in other tabs.")
        else:
            st.success("Formula sums to 100%.")
    else:
        st.info("Add ingredients above to start building a formula.")

    with st.expander("📖 View full ingredient database"):
        st.dataframe(ingredients_df, use_container_width=True)

# --------------------------------------------------------------------------
# TAB 2 - Compatibility
# --------------------------------------------------------------------------
with tab_compat:
    st.subheader("Ingredient compatibility check")
    if not st.session_state.formula:
        st.info("Add ingredients in Tab 1 first.")
    else:
        flags = sort_flags(check_compatibility(formula_df(), incompat_data))
        high = [f for f in flags if f["severity"] == "high"]
        med = [f for f in flags if f["severity"] == "medium"]
        low = [f for f in flags if f["severity"] == "low"]

        c1, c2, c3 = st.columns(3)
        c1.metric("High severity", len(high))
        c2.metric("Medium severity", len(med))
        c3.metric("Low severity", len(low))

        if not flags:
            st.success("No known conflicts found among these ingredients in the sample rule set.")
        for f in flags:
            icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(f["severity"], "⚪")
            st.markdown(f"{icon} **{f['a']} + {f['b']}** — *{f['severity']} severity*  \n{f['reason']}")

        st.caption("This checks a curated sample rule set of well-known interactions - it is not exhaustive. Use the AI Assistant tab for a broader qualitative read.")

# --------------------------------------------------------------------------
# TAB 3 - Properties
# --------------------------------------------------------------------------
with tab_props:
    st.subheader("Estimated physical properties")
    if not st.session_state.formula:
        st.info("Add ingredients in Tab 1 first.")
    else:
        fdf = formula_df()
        flags = check_compatibility(fdf, incompat_data)

        ph, ph_contributors = estimate_ph(fdf, ingredients_df)
        visc = estimate_viscosity(fdf, ingredients_df)
        stability_score, stability_notes = estimate_stability(fdf, ingredients_df, flags)

        c1, c2, c3 = st.columns(3)
        c1.metric("Estimated pH", ph if ph is not None else "n/a")
        c2.metric("Texture", visc["texture_estimate"])
        c3.metric("Stability score", f"{stability_score}/100")

        st.progress(stability_score / 100)

        if stability_notes:
            st.markdown("**Stability notes:**")
            for n in stability_notes:
                st.markdown(f"- {n}")

        if visc["phase_note"]:
            st.warning(visc["phase_note"])

        with st.expander("How the pH estimate was calculated"):
            if ph_contributors:
                contrib_df = pd.DataFrame(ph_contributors, columns=["Ingredient", "Typical pH midpoint", "Percent in formula"])
                st.dataframe(contrib_df, use_container_width=True)
            else:
                st.write("No ingredients in this formula have a defined pH range.")

        with st.expander("How the texture/viscosity estimate was calculated"):
            st.write(f"Water phase: {visc['water_phase_percent']}% · Oil phase: {visc['oil_phase_percent']}%")
            if visc["contributing_ingredients"]:
                vc_df = pd.DataFrame(visc["contributing_ingredients"], columns=["Ingredient", "Category", "Thickening contribution"])
                st.dataframe(vc_df, use_container_width=True)

        st.caption("Heuristic estimates for directional R&D use only - always confirm with a calibrated pH meter, viscometer, and a real stability protocol (accelerated aging, freeze-thaw, centrifuge).")

# --------------------------------------------------------------------------
# TAB 4 - Regulatory
# --------------------------------------------------------------------------
with tab_reg:
    st.subheader(f"Regulatory check — {region}")
    if not st.session_state.formula:
        st.info("Add ingredients in Tab 1 first.")
    else:
        results = check_regulatory(formula_df(), ingredients_df, region)
        verdict = summarize(results)
        verdict_map = {
            "compliant": ("🟢 Compliant (per sample data)", "success"),
            "needs_review": ("🟡 Needs manual review", "warning"),
            "non_compliant": ("🔴 Non-compliant (per sample data)", "error"),
        }
        label, kind = verdict_map[verdict]
        getattr(st, kind)(label)

        res_df = pd.DataFrame(results)
        status_icon = {"ok": "✅", "banned": "⛔", "over_limit": "⚠️", "unknown": "❓"}
        res_df["status"] = res_df["status"].map(lambda s: f"{status_icon.get(s,'')} {s}")
        st.dataframe(res_df, use_container_width=True)

        st.caption(
            "Sample regulatory data currently covers a curated subset of ingredients "
            "(including the EU's 2024/996 retinol/arbutin limits). Anything marked "
            "'unknown' or not listed needs manual verification against the official "
            "regional source before use in a real product."
        )

# --------------------------------------------------------------------------
# TAB 5 - Cost & Sustainability
# --------------------------------------------------------------------------
with tab_cost:
    st.subheader("Cost breakdown & greener/cheaper swaps")
    if not st.session_state.formula:
        st.info("Add ingredients in Tab 1 first.")
    else:
        cost_result = calculate_cost(formula_df(), ingredients_df, batch_size_kg)

        c1, c2 = st.columns(2)
        c1.metric(f"Total batch cost ({batch_size_kg} kg)", f"${cost_result['total_cost_usd']:,.2f}")
        c2.metric("Cost per kg", f"${cost_result['cost_per_kg_batch_usd']:,.4f}")

        line_df = pd.DataFrame(cost_result["line_items"]).sort_values("line_cost_usd", ascending=False)
        st.dataframe(line_df, use_container_width=True)
        st.bar_chart(line_df.set_index("inci_name")["line_cost_usd"])

        if cost_result["missing_from_db"]:
            st.warning(f"Not priced (missing from sample DB): {', '.join(cost_result['missing_from_db'])}")

        st.divider()
        st.markdown("**Substitute suggestions** (same function, cheaper and/or more sustainable)")
        target = st.selectbox("Find alternatives for", [r["inci_name"] for r in st.session_state.formula])
        subs = find_substitutes(target, ingredients_df)
        if subs:
            st.dataframe(pd.DataFrame(subs), use_container_width=True)
        else:
            st.info("No same-category alternatives found in the sample database for this ingredient.")

# --------------------------------------------------------------------------
# TAB 6 - AI Assistant
# --------------------------------------------------------------------------
with tab_ai:
    st.subheader("Ask the AI formulation assistant")
    st.caption("Groq-powered narrative layer. It reasons over the numbers computed in the other tabs - it does not invent new regulatory limits.")

    if not groq_api_key:
        st.info("Enter your Groq API key in the sidebar to enable this tab.")
    elif not st.session_state.formula:
        st.info("Add ingredients in Tab 1 first.")
    else:
        fdf = formula_df()
        flags = check_compatibility(fdf, incompat_data)
        ph, _ = estimate_ph(fdf, ingredients_df)
        visc = estimate_viscosity(fdf, ingredients_df)
        stability_score, stability_notes = estimate_stability(fdf, ingredients_df, flags)
        reg_results = check_regulatory(fdf, ingredients_df, region)
        cost_result = calculate_cost(fdf, ingredients_df, batch_size_kg)

        context_blob = {
            "formula": st.session_state.formula,
            "estimated_ph": ph,
            "texture_estimate": visc["texture_estimate"],
            "stability_score": stability_score,
            "stability_notes": stability_notes,
            "compatibility_flags": flags,
            "regulatory_region": region,
            "regulatory_results": reg_results,
            "total_cost_usd": cost_result["total_cost_usd"],
            "batch_size_kg": batch_size_kg,
        }

        default_question = "Review this formula. Call out any red flags, and suggest 1-2 concrete improvements for stability, cost, or sustainability."
        user_question = st.text_area("Your question", value=default_question, height=100)

        if st.button("🤖 Ask AI"):
            prompt = (
                f"Computed formulation data (JSON):\n{json.dumps(context_blob, indent=2)}\n\n"
                f"Chemist's question: {user_question}"
            )
            with st.spinner("Thinking..."):
                try:
                    reply = call_groq(groq_api_key, groq_model, prompt)
                    st.markdown(reply)
                except GroqError as e:
                    st.error(str(e))
