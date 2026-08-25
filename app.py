import base64
import io
import json
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from PIL import Image

load_dotenv()  # picks up a local .env file if present; no-op if it doesn't exist

from utils.ai_client import (
    AIError, AIRateLimitError, GEMINI_MODELS, GEMINI_SIGNUP_URL, GEMINI_FREE_TIER_NOTE,
    GEMINI_KEY_ENV_VARS, WEB_SEARCH_CAPABLE_MODELS, make_call_fn, call_gemini_with_fallback,
)
from utils.property_estimator import estimate_ph, estimate_viscosity, estimate_stability
from utils.compatibility_checker import check_compatibility, sort_flags
from utils.regulatory_checker import check_regulatory, summarize, REGIONS
from utils.cost_calculator import calculate_cost, find_substitutes, calculate_unit_economics, units_from_batch
from utils.inhouse_materials import (
    parse_inhouse_upload, generate_template_bytes, merge_material_sources,
    merge_inhouse_upload, add_manual_material, empty_inhouse_df, InHouseParseError,
)
from utils.formula_ai import (
    build_candidate_context, build_incompat_context, generate_formula, refine_formula,
    validate_and_normalize, FormulaGenerationError, search_worldwide_ingredients, web_results_to_candidate_rows,
)
from utils.currency import CURRENCY_OPTIONS, currency_label, format_money
from utils.technical_profile import compute_technical_profile, render_technical_description

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")


def _load_favicon():
    """Use the CosmoGen icon as the browser-tab favicon; fall back to an
    emoji if the asset is ever missing so the app never crashes over branding."""
    try:
        return Image.open(os.path.join(ASSETS_DIR, "cosmogen_favicon.png"))
    except Exception:
        return "🧴"


st.set_page_config(page_title="CosmoGen | AI Cosmetic Formulation Studio", page_icon=_load_favicon(), layout="wide")


@st.cache_data
def _load_logo_base64():
    """Base64-encode the CosmoGen icon+wordmark lockup once so it can be
    embedded directly in the hero banner HTML. This is the actual provided
    logo artwork (cropped to the icon+wordmark, since the baked-in tagline
    text is too thin to stay legible at hero-banner scale - a separately
    rendered tagline sits next to it instead, see the hero banner markup)."""
    try:
        with open(os.path.join(ASSETS_DIR, "cosmogen_wordmark.png"), "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None


PRODUCT_CATEGORIES = {
    "Skincare": ["Face Moisturizer / Cream", "Face Serum", "Facial Cleanser", "Toner",
                 "Eye Cream", "Sunscreen / SPF", "Face Mask", "Body Lotion", "Body Wash"],
    "Color Cosmetics": ["Foundation", "Concealer", "Lipstick", "Lip Gloss / Balm",
                         "Blush", "Eyeshadow", "Mascara", "Eyeliner"],
    "Haircare": ["Shampoo", "Conditioner", "Hair Serum / Oil", "Hair Mask", "Styling Gel / Cream"],
    "Personal Care": ["Deodorant", "Hand Cream", "Body Butter", "Baby Care Lotion"],
}

REGION_METHODOLOGY = {
    "EU": {
        "framework": "Regulation (EC) No 1223/2009 (the EU Cosmetics Regulation) and its annexes, "
                     "cross-referenced against the European Commission's CosIng database.",
        "assumptions": [
            "Limits shown are for the finished, ready-to-use product concentration, not raw material purity.",
            "Where EU rules differ for leave-on vs. rinse-off products, only the more common/conservative "
            "case is captured unless the ingredient's note says otherwise.",
            "Labeling duties (e.g. declaring the 26 EU fragrance allergens) and claims-substantiation rules "
            "are NOT checked here - only ingredient permission/concentration.",
        ],
    },
    "US": {
        "framework": "FDA cosmetic ingredient rules (21 CFR) and, where relevant, OTC drug monographs "
                     "(e.g. for sunscreen actives or acne treatments marketed as OTC drugs).",
        "assumptions": [
            "The US does not pre-approve most cosmetic ingredients the way the EU does, so 'ok' here "
            "usually means 'no specific FDA prohibition/limit found in this sample data' rather than "
            "an affirmative approval.",
            "OTC monograph concentration limits (e.g. sunscreen actives) are only reflected for the "
            "ingredients explicitly annotated as such.",
        ],
    },
    "India": {
        "framework": "Bureau of Indian Standards (BIS) cosmetic standards and CDSCO (Central Drugs "
                     "Standard Control Organisation) rules.",
        "assumptions": [
            "India's rules substantially mirror the EU's in many areas; where this sample data marks "
            "an India limit as 'aligned with EU', that's a reasonable-but-unverified assumption, not "
            "a confirmed independent citation.",
        ],
    },
}

STATUS_LEGEND = {
    "ok": ("✅", "Allowed in this sample data at the concentration used - no flagged restriction found."),
    "over_limit": ("⚠️", "Allowed, but the formula's concentration exceeds the maximum this sample data has on file for the region."),
    "banned": ("⛔", "This sample data marks the ingredient as not permitted in this region's cosmetics."),
    "unknown": ("❓", "No regulatory data on file for this ingredient/region in this sample database - needs manual verification. Common for in-house/custom materials."),
}

# --------------------------------------------------------------------------
# Premium visual styling - palette sampled from the CosmoGen logo
# (deep navy #05061c -> #0d1250 background, purple #8154FC -> cyan #4ADAFD mark)
# --------------------------------------------------------------------------
CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@600;700;800&family=Inter:wght@400;500;600;700&display=swap');

html, body, [class*="css"]  { font-family: 'Inter', sans-serif; }
h1, h2, h3 { font-family: 'Poppins', sans-serif !important; font-weight: 700 !important; }

.hero-banner {
    background: linear-gradient(135deg, #05061c 0%, #0d1250 55%, #1a1345 100%);
    padding: 1.5rem 2.4rem; border-radius: 20px; margin-bottom: 1.4rem;
    box-shadow: 0 10px 30px rgba(13, 18, 80, 0.35);
    display: flex; align-items: center; gap: 1.6rem;
}
.hero-banner img.hero-logo { height: 100px; flex-shrink: 0; }
.hero-title-group .hero-tagline { color: #9fa8d9; font-size: 0.85rem; letter-spacing: 2px;
    text-transform: uppercase; margin: 0 0 0.5rem 0; font-weight: 600; }
.hero-title-group p.hero-sub { color: #b9c2ea; font-size: 0.95rem; margin: 0; }

.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
    background: linear-gradient(135deg, #8154FC, #4ADAFD); color: #ffffff; border: none;
    border-radius: 8px; font-weight: 600; padding: 0.5rem 1.2rem; transition: all .15s ease;
    min-height: 42px;
}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
    box-shadow: 0 4px 16px rgba(129, 84, 252, 0.45); transform: translateY(-1px);
}
.stButton > button[kind="secondary"] { background: #171344; color: #d9e0ff; }

[data-testid="stMetricValue"] { font-family: 'Poppins', sans-serif; color: #171344; }

.badge { display:inline-block; padding:0.25rem 0.75rem; border-radius:999px; font-size:0.78rem;
    font-weight:600; margin:0.15rem 0.3rem 0.15rem 0; }
.badge-premium { background:#efe6ff; color:#6b32d6; }
.badge-midrange { background:#e0f2fe; color:#0e6ba8; }
.badge-budget { background:#e3f7ee; color:#1f8a53; }
.badge-inhouse { background:#eee; color:#444; }
.badge-worldwide { background:#e0f7fb; color:#0e7c92; }
.badge-high { background:#fbe1e1; color:#9c1f1f; }
.badge-medium { background:#fdecd2; color:#97590a; }
.badge-low { background:#fdf6d8; color:#8a7411; }
.badge-none { background:#e3f2e6; color:#1f6b34; }

.claim-pill { display:inline-block; background:#171344; color:#c9e8ff; padding:0.3rem 0.9rem;
    border-radius:999px; font-size:0.85rem; margin:0.2rem 0.3rem 0.2rem 0; }

.result-card { background:#f7f5ff; border:1px solid #e3daff; border-radius:14px;
    padding:1.3rem 1.5rem; margin-bottom:1rem; word-wrap: break-word; overflow-wrap: break-word; }
.step-label { color:#8154FC; font-weight:700; letter-spacing:1px; font-size:0.8rem;
    text-transform:uppercase; margin-bottom:-0.4rem; }
.version-pill { display:inline-block; background:#f0edfd; color:#524a7a; padding:0.2rem 0.7rem;
    border-radius:999px; font-size:0.78rem; margin:0 0.3rem 0.3rem 0; border:1px solid #d8ccf7; }
.version-pill.active { background:#171344; color:#c9e8ff; border-color:#171344; }

/* Tables/dataframes scroll horizontally instead of squashing columns unreadably */
[data-testid="stDataFrame"], [data-testid="stTable"] { overflow-x: auto; -webkit-overflow-scrolling: touch; }

/* ===================== Mobile responsiveness ===================== */
@media (max-width: 768px) {
    /* Reclaim Streamlit's default side padding for more usable width on small screens.
       Target both the stable testid and the legacy class name as a fallback, since
       Streamlit's internal class names can change between versions. */
    [data-testid="stMainBlockContainer"], .block-container { padding-left: 1rem !important; padding-right: 1rem !important; padding-top: 1.5rem !important; }

    /* Hero banner: stack logo above text instead of cramming them side by side */
    .hero-banner {
        flex-direction: column; align-items: flex-start; text-align: left;
        padding: 1.1rem 1.3rem; gap: 0.7rem; border-radius: 16px;
    }
    .hero-banner img.hero-logo { height: 60px; }
    .hero-title-group .hero-tagline { font-size: 0.68rem; letter-spacing: 1.5px; }
    .hero-title-group p.hero-sub { font-size: 0.82rem; line-height: 1.4; }

    /* Headings scale down so they don't dominate a small screen */
    h1 { font-size: 1.45rem !important; }
    h2 { font-size: 1.2rem !important; }
    h3 { font-size: 1.02rem !important; }

    /* Buttons: full width with a comfortable tap target (44px is the accepted minimum) */
    .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
        width: 100%; min-height: 44px; padding: 0.6rem 1rem; font-size: 0.95rem;
    }

    /* Metrics: desktop-sized numbers are oversized and wrap awkwardly on narrow screens */
    [data-testid="stMetricValue"] { font-size: 1.3rem !important; }
    [data-testid="stMetricLabel"] { font-size: 0.76rem !important; }

    /* Cards & pills: tighter padding/text so content fits without excessive wrapping */
    .result-card { padding: 1rem 1.1rem; border-radius: 12px; }
    .badge { font-size: 0.72rem; padding: 0.2rem 0.6rem; }
    .claim-pill { font-size: 0.78rem; padding: 0.25rem 0.7rem; }
    .version-pill { font-size: 0.72rem; padding: 0.18rem 0.6rem; }
    .step-label { font-size: 0.72rem; }

    /* Form inputs: comfortable tap-target height, readable text size (16px avoids iOS auto-zoom-on-focus) */
    .stTextInput input, .stNumberInput input, .stTextArea textarea,
    .stSelectbox [data-baseweb="select"] > div { min-height: 42px; font-size: 16px; }

    /* Radio groups (several are horizontal=True) get tighter gaps so options wrap cleanly */
    .stRadio > div { gap: 0.4rem !important; row-gap: 0.5rem !important; }
}

@media (max-width: 480px) {
    .hero-banner img.hero-logo { height: 48px; }
    h1 { font-size: 1.25rem !important; }
    [data-testid="stMetricValue"] { font-size: 1.1rem !important; }
    .result-card { padding: 0.85rem 0.9rem; }
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


def badge(text, css_class):
    return f'<span class="badge {css_class}">{text}</span>'


def positioning_badge(tier):
    cls = {"Premium": "badge-premium", "Mid-Range": "badge-midrange", "Budget": "badge-budget"}.get(tier, "badge-midrange")
    icon = {"Premium": "💎", "Mid-Range": "⚖️", "Budget": "💰"}.get(tier, "")
    return badge(f"{icon} {tier}", cls)


def severity_badge(sev):
    cls = {"high": "badge-high", "medium": "badge-medium", "low": "badge-low", "none": "badge-none"}.get(sev, "badge-none")
    return badge(sev, cls)


def split_texture_label(full_text: str):
    """Split the descriptive texture string into a short headline and the
    full text, used by render_texture_block below."""
    if not full_text:
        return full_text, full_text
    if " (" in full_text:
        short, _ = full_text.split(" (", 1)
        return short.strip(), full_text
    return full_text, full_text


def render_texture_block(container, texture_full: str):
    """st.metric() truncates long values with a hidden ellipsis - render
    texture as a metric-styled block that wraps instead, so the full
    description is always directly visible with no truncation and no
    hover needed."""
    short, full = split_texture_label(texture_full)
    container.markdown(
        f"""<div style="margin-bottom:0.3rem;">
            <div style="font-size:0.875rem; color:#5b5b5b;">Texture</div>
            <div style="font-size:1.35rem; font-weight:700; color:#171344; font-family:'Poppins',sans-serif; line-height:1.3;">{short}</div>
            <div style="font-size:0.8rem; color:#6b7280; margin-top:0.2rem; line-height:1.35;">{full}</div>
        </div>""",
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
@st.cache_data
def load_worldwide_ingredients():
    return pd.read_csv(os.path.join(BASE_DIR, "data", "ingredients.csv"))


@st.cache_data
def load_incompatibilities():
    with open(os.path.join(BASE_DIR, "data", "incompatibilities.json")) as f:
        return json.load(f)


worldwide_df = load_worldwide_ingredients()
incompat_data = load_incompatibilities()

if "formula" not in st.session_state:
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
if "inhouse_df" not in st.session_state:
    st.session_state.inhouse_df = empty_inhouse_df()
if "ai_formula_result" not in st.session_state:
    st.session_state.ai_formula_result = None
if "formula_versions" not in st.session_state:
    st.session_state.formula_versions = []  # list of {"label": str, "result": {...}}
if "active_version_idx" not in st.session_state:
    st.session_state.active_version_idx = None


def formula_df():
    return pd.DataFrame(st.session_state.formula)


def get_converted_worldwide_df(fx_rate: float = 1.0):
    """Worldwide DB costs are authored in USD; convert to the selected
    currency using the sidebar's exchange rate (1.0 = no conversion, i.e. USD)."""
    ww = worldwide_df.copy()
    if fx_rate and fx_rate != 1.0:
        ww["cost_per_kg_usd"] = ww["cost_per_kg_usd"] * fx_rate
    return ww


def get_working_ingredients_df(fx_rate: float = 1.0):
    """The full lookup table (worldwide + whatever in-house materials the user
    has entered) used by every tab so ingredient names always resolve,
    regardless of which sourcing strategy an AI-generated formula used."""
    return merge_material_sources(get_converted_worldwide_df(fx_rate), st.session_state.inhouse_df, "In-House + Worldwide")


def get_candidate_df(source_strategy: str, fx_rate: float = 1.0):
    return merge_material_sources(get_converted_worldwide_df(fx_rate), st.session_state.inhouse_df, source_strategy)


def augment_with_web_search(candidate_df, call_fn, source_strategy, product_category,
                             product_subtype, description, positioning, fx_rate, status_obj):
    """
    If the sourcing strategy includes Worldwide AND a search-capable model is
    available, ask it to find real, currently-relevant worldwide ingredients
    beyond the static database and fold them into the candidate pool (tagged
    'Worldwide (Web)', with AI-estimated costs clearly caveated as such).
    Best-effort: any failure just falls back to the static database with an
    info note, never blocks formula generation.

    Currently WEB_SEARCH_CAPABLE_MODELS is empty (see utils/ai_client.py) so
    this always takes the graceful-fallback path - kept in place, ready to
    re-enable if a confirmed-search-capable Gemini model is added later.

    Returns (expanded_candidate_df, web_ingredients_list, note_message_or_None).
    """
    if "Worldwide" not in source_strategy:
        return candidate_df, [], None
    if not WEB_SEARCH_CAPABLE_MODELS:
        return candidate_df, [], "Using the built-in worldwide database only (live web-sourced ingredient search is temporarily unavailable)."

    status_obj.update(label="Searching worldwide ingredient sources...")
    try:
        web_ingredients = search_worldwide_ingredients(
            call_fn, product_category, product_subtype, description, positioning,
            on_retry=make_status_retry_callback(status_obj, "Searching worldwide ingredient sources..."),
        )
    except (AIError, FormulaGenerationError) as e:
        return candidate_df, [], f"Web ingredient search didn't return results this time ({e}) - continuing with the built-in database."

    rows = web_results_to_candidate_rows(web_ingredients, list(candidate_df.columns))
    if not rows:
        return candidate_df, [], "Web search didn't surface any usable new ingredients this time - continuing with the built-in database."

    web_df = pd.DataFrame(rows)
    if fx_rate and fx_rate != 1.0:
        web_df["cost_per_kg_usd"] = web_df["cost_per_kg_usd"] * fx_rate
    web_df["source"] = "Worldwide (Web)"
    expanded = pd.concat([candidate_df, web_df], ignore_index=True)
    expanded = expanded.drop_duplicates(subset="inci_name", keep="first")
    status_obj.update(label=f"Found {len(rows)} additional worldwide ingredient(s) via web search - formulating...")
    return expanded, web_ingredients, None


def make_status_retry_callback(status_obj, base_label):
    """Feeds live rate-limit/retry progress into a st.status() box instead of
    the user just seeing a frozen spinner during a 429 backoff. `reason`
    already includes the key/model label (e.g. "Gemini rate limit")."""
    def _on_retry(attempt, max_attempts, wait_seconds, reason):
        status_obj.update(label=f"{base_label} — {reason}, retrying in {wait_seconds:.0f}s (attempt {attempt}/{max_attempts})...")
    return _on_retry


def make_status_fallback_callback(status_obj, base_label):
    """Feeds a live 'switching key/model' message into a st.status() box when
    the current attempt fails and the app automatically moves to the next
    one in the chain (a different model on the same key, or the second key)."""
    def _on_fallback(from_label, to_label, reason):
        status_obj.update(label=f"{base_label} — {from_label} unavailable, switching to {to_label}...")
    return _on_fallback


# --------------------------------------------------------------------------
# Sidebar - Gemini API keys (dual-key with automatic fallback, no model
# selection - the app always tries GEMINI_MODELS in order automatically),
# currency, region, batch size
# --------------------------------------------------------------------------
def get_default_key(key_env_var: str) -> str:
    """Resolve a default API key for a given env var name with this priority:
    1. st.secrets (Streamlit Community Cloud's Settings -> Secrets)
    2. environment variable (from a local .env file via python-dotenv, or the shell)
    3. empty string (user pastes one into the sidebar manually)
    """
    try:
        secret_val = st.secrets.get(key_env_var, "")
        if secret_val:
            return secret_val
    except Exception:
        pass
    return os.environ.get(key_env_var, "")


def render_key_input(field_label: str, key_env_var: str, widget_key: str) -> str:
    """'Never show a configured secret' pattern: if a key is set via Secrets/.env,
    show only a confirmation - never the value itself, even in a password field.
    A checkbox lets the user type a different key for just this session."""
    default_key = get_default_key(key_env_var)
    if default_key:
        st.success(f"✅ {field_label}: configured (via Secrets/.env)")
        use_override = st.checkbox(f"Use a different {field_label.lower()} for this session", key=f"{widget_key}_override_checkbox")
        if use_override:
            override_key = st.text_input(
                f"Your {field_label}", type="password", key=f"{widget_key}_override_key",
                help="Overrides the configured key for this browser session only. Never written to disk.",
            )
            return override_key or default_key
        return default_key
    else:
        st.caption(f"Not configured. Set `{key_env_var}` in Secrets/.env, or enter one below for this session only.")
        return st.text_input(field_label, type="password", key=f"{widget_key}_manual_key")


with st.sidebar:
    st.header("⚙️ Settings")

    st.markdown("**AI Provider: Google Gemini**")
    st.caption(
        "No model to pick - the app automatically uses gemini-3.5-flash-lite by default and "
        "switches to another Gemini model on its own if that one is ever deprecated, incompatible, "
        "rate-limited, or overloaded. Configure a second key below and it'll switch to that "
        "automatically too if the first is exhausted - nothing for you to do when it happens."
    )

    with st.expander("Gemini API key(s)", expanded=True):
        gemini_key_1 = render_key_input("Gemini API key", GEMINI_KEY_ENV_VARS[0], "gemini_key1")
        st.divider()
        gemini_key_2 = render_key_input("Gemini API key (backup, optional)", GEMINI_KEY_ENV_VARS[1], "gemini_key2")
        st.caption(f"{GEMINI_FREE_TIER_NOTE} [Get a key]({GEMINI_SIGNUP_URL})")

    api_keys = [gemini_key_1, gemini_key_2]
    ai_available = bool(gemini_key_1 or gemini_key_2)
    if not ai_available:
        st.warning("⚠️ No Gemini API key configured - AI features are disabled until you add one above.")
    elif gemini_key_1 and gemini_key_2:
        st.caption("✅ Two keys configured - automatic fallback between them is active.")

    st.divider()

    currency_code = st.selectbox(
        "Currency", list(CURRENCY_OPTIONS.keys()), format_func=currency_label,
        index=list(CURRENCY_OPTIONS.keys()).index("INR"),
        help="Applied to all cost displays and calculations across the app.",
    )
    currency_symbol = CURRENCY_OPTIONS[currency_code]["symbol"]
    if currency_code != "USD":
        _default_rate = CURRENCY_OPTIONS[currency_code]["approx_rate_per_usd"]
        fx_rate = st.number_input(
            f"Exchange rate (1 USD = ? {currency_code})", min_value=0.0001, value=_default_rate, step=0.01, format="%.4f",
            help="Approximate default - edit with today's actual rate for accuracy. Only converts the "
                 "worldwide (USD-authored) ingredient database; your in-house costs are used exactly as entered.",
        )
    else:
        fx_rate = 1.0

    st.divider()
    region = st.selectbox("Regulatory region", list(REGIONS.keys()), index=list(REGIONS.keys()).index("India"))
    batch_size_kg = st.number_input("Batch size (kg)", min_value=0.1, value=1.0, step=0.5,
                                     help="Small trial batches are common for R&D - defaults to 1 kg. Increase for production-scale runs.")
    st.divider()
    st.caption(
        "⚠️ Sample regulatory & cost data are illustrative starting points for R&D "
        "exploration, not a certified compliance database. Verify against official "
        "EU CosIng, US FDA, and India BIS/CDSCO sources before any real submission."
    )


def fmt(value, decimals=2):
    return format_money(value, currency_symbol, decimals)


_logo_b64 = _load_logo_base64()
if _logo_b64:
    _logo_tag = f'<img class="hero-logo" src="data:image/png;base64,{_logo_b64}" alt="CosmoGen logo">'
else:
    _logo_tag = '<h1 style="color:#f7f3ff;font-size:2rem;margin:0;font-family:\'Poppins\',sans-serif;">CosmoGen</h1>'

st.markdown(
    f"""
    <div class="hero-banner">
        {_logo_tag}
        <div class="hero-title-group">
            <p class="hero-tagline">AI Cosmetic Formulation Studio</p>
            <p class="hero-sub">Formula Predictor · Property Estimator · Regulatory Rule Checker · Cost & Sustainability Calculator</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

tab_studio, tab_build, tab_compat, tab_props, tab_reg, tab_cost, tab_ai = st.tabs(
    ["✨ Formula Studio", "🧪 Manual Builder", "🔬 Compatibility", "📊 Properties",
     "⚖️ Regulatory", "💰 Cost & Sustainability", "💬 AI Assistant"]
)

# ==========================================================================
# TAB: FORMULA STUDIO (AI-driven formula development)
# ==========================================================================
with tab_studio:
    st.markdown('<p class="step-label">Step 1</p>', unsafe_allow_html=True)
    st.subheader("Material sourcing")

    with st.container(border=True):
        st.markdown("**Your in-house materials**")
        upload_col, template_col = st.columns([2, 1])
        with upload_col:
            uploaded = st.file_uploader("Upload material list (.xlsx or .csv)", type=["xlsx", "xls", "csv"], key="inhouse_uploader")
        with template_col:
            st.write("")
            st.write("")
            st.download_button(
                "⬇️ Download template", data=generate_template_bytes(),
                file_name="inhouse_materials_template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )

        if uploaded is not None:
            try:
                parsed_df, parse_warnings = parse_inhouse_upload(uploaded)
                before_count = len(st.session_state.inhouse_df)
                st.session_state.inhouse_df = merge_inhouse_upload(st.session_state.inhouse_df, parsed_df)
                added_or_updated = len(parsed_df)
                st.success(
                    f"Merged {added_or_updated} material(s) from {uploaded.name} "
                    f"(matching names updated, others kept) - now {len(st.session_state.inhouse_df)} total."
                )
                for w in parse_warnings:
                    st.warning(w)
            except InHouseParseError as e:
                st.error(str(e))

        st.caption(f"Or add/edit materials directly below (Material Name and Cost/kg required; costs are treated as already being in {currency_symbol} {currency_code}). The table below spans the full width and scrolls horizontally if needed - no column is hidden.")
        edited_inhouse = st.data_editor(
            st.session_state.inhouse_df, num_rows="dynamic", width="stretch",
            key="inhouse_editor",
            column_config={
                "inci_name": st.column_config.TextColumn("Material Name", required=True, width="medium"),
                "category": st.column_config.TextColumn("Category", width="medium"),
                "function": st.column_config.TextColumn("Function", width="medium"),
                "cost_per_kg_usd": st.column_config.NumberColumn(f"Cost/kg ({currency_symbol})", min_value=0.0, step=0.1, width="small"),
                "typical_ph_min": st.column_config.NumberColumn("pH Min", min_value=0.0, max_value=14.0, step=0.1, width="small"),
                "typical_ph_max": st.column_config.NumberColumn("pH Max", min_value=0.0, max_value=14.0, step=0.1, width="small"),
                "sustainability_score": st.column_config.NumberColumn("Sustainability (1-10)", min_value=1, max_value=10, step=1, width="small"),
                "stock_available_kg": st.column_config.NumberColumn("Stock (kg)", min_value=0.0, step=1.0, width="small"),
                "notes": st.column_config.TextColumn("Notes", width="large"),
            },
        )
        st.session_state.inhouse_df = edited_inhouse

        st.divider()
        strat_col, metric_col1, metric_col2 = st.columns([2, 1, 1])
        with strat_col:
            st.markdown("**Sourcing strategy**")
            source_strategy = st.radio(
                "Which materials should the AI draw from?",
                ["In-House", "Worldwide", "In-House + Worldwide"], index=2, key="source_strategy", horizontal=True,
            )
        n_inhouse = len(st.session_state.inhouse_df)
        n_worldwide = len(worldwide_df)
        metric_col1.metric("In-house materials", n_inhouse)
        metric_col2.metric("Worldwide database", n_worldwide)
        if source_strategy in ("In-House", "In-House + Worldwide") and n_inhouse == 0:
            st.warning("Add or upload at least one in-house material, or switch strategy to Worldwide.")
        if "Worldwide" in source_strategy and not WEB_SEARCH_CAPABLE_MODELS:
            st.caption("ℹ️ Using the built-in worldwide database only (live web-sourced ingredient search is temporarily unavailable).")

    st.markdown('<p class="step-label">Step 2</p>', unsafe_allow_html=True)
    st.subheader("Product brief")

    with st.container(border=True):
        b1, b2 = st.columns(2)
        with b1:
            product_category = st.selectbox("Product category", list(PRODUCT_CATEGORIES.keys()))
            product_subtype = st.selectbox("Product type", PRODUCT_CATEGORIES[product_category])
        with b2:
            positioning_choice = st.radio(
                "Positioning", ["💰 Budget", "⚖️ Mid-Range", "💎 Premium"], index=1, horizontal=True, key="positioning_choice",
            )
            positioning = positioning_choice.split(" ", 1)[1]

        description = st.text_area(
            "Describe the desired product",
            placeholder="e.g. Lightweight, fast-absorbing vitamin C brightening serum for oily/combination skin, "
                        "fragrance-free, silicone-free, targets dullness and uneven tone.",
            height=90,
        )

    st.markdown('<p class="step-label">Step 3</p>', unsafe_allow_html=True)
    st.subheader("Generate")

    generate_clicked = st.button("✨ Generate AI Formula", type="primary", width="content")

    if generate_clicked:
        if not ai_available:
            st.error("No Gemini API key configured. Add one in the sidebar, or configure Secrets/.env.")
        elif not description.strip():
            st.error("Describe the desired product before generating.")
        else:
            candidate_df = get_candidate_df(source_strategy, fx_rate)
            if candidate_df.empty:
                st.error("No candidate materials available for this sourcing strategy - add in-house materials or switch strategy.")
            else:
                with st.status("Formulating... the AI is designing your formula", expanded=False) as status:
                    try:
                        call_fn = make_call_fn(api_keys, on_fallback=make_status_fallback_callback(status, "Formulating..."))
                        candidate_df, web_ingredients, web_note = augment_with_web_search(
                            candidate_df, call_fn, source_strategy, product_category,
                            product_subtype, description, positioning, fx_rate, status,
                        )
                        candidate_ingredients = build_candidate_context(candidate_df)
                        incompat_rules = build_incompat_context(incompat_data)
                        status.update(label="Formulating... the AI is designing your formula")
                        raw = generate_formula(
                            call_fn, product_category, product_subtype,
                            description, positioning, source_strategy, candidate_ingredients, incompat_rules,
                            currency_code=currency_code, on_retry=make_status_retry_callback(status, "Formulating..."),
                        )
                        candidate_names = set(candidate_df["inci_name"])
                        worldwide_names = set(worldwide_df["inci_name"])
                        flat, phases_out, warnings, meta = validate_and_normalize(raw, candidate_names, worldwide_names)
                        new_result = {
                            "flat": flat, "phases": phases_out, "warnings": warnings, "meta": meta,
                            "candidate_df": candidate_df, "region": region, "positioning": positioning,
                            "product_category": product_category, "product_subtype": product_subtype,
                            "description": description, "source_strategy": source_strategy,
                            "refinement_history": [], "web_ingredients": web_ingredients, "web_note": web_note,
                        }
                        st.session_state.ai_formula_result = new_result
                        st.session_state.formula_versions = [{"label": "v1 · Initial", "result": new_result}]
                        st.session_state.active_version_idx = 0
                        status.update(label="Formula generated!", state="complete")
                    except AIError as e:
                        status.update(label="Generation failed", state="error")
                        st.error(str(e))
                    except FormulaGenerationError as e:
                        status.update(label="Generation failed", state="error")
                        st.error(str(e))

    result = st.session_state.ai_formula_result
    if result:
        st.divider()

        # --- Version history selector (if there's more than one version) ---
        if len(st.session_state.formula_versions) > 1:
            st.markdown("**Version history**")
            v_labels = [v["label"] for v in st.session_state.formula_versions]
            picked_idx = st.radio(
                "Viewing version:", list(range(len(v_labels))), index=st.session_state.active_version_idx,
                format_func=lambda i: v_labels[i], horizontal=True, key="version_picker", label_visibility="collapsed",
            )
            if picked_idx != st.session_state.active_version_idx:
                st.session_state.active_version_idx = picked_idx
                st.session_state.ai_formula_result = st.session_state.formula_versions[picked_idx]["result"]
                st.rerun()

        result = st.session_state.ai_formula_result
        meta = result["meta"]
        flat = result["flat"]
        phases = result["phases"]
        candidate_df = result["candidate_df"]
        r_flat_df = pd.DataFrame(flat)

        st.markdown(
            f"""
            <div class="result-card">
                <h2 style="margin-top:0;">{meta['formula_name']}</h2>
                {positioning_badge(result['positioning'])} {badge(result['product_category'] + ' · ' + result['product_subtype'], 'badge-worldwide')}
                <p style="margin-top:0.8rem; font-size:1.02rem;">{meta['product_summary']}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if meta["key_claims"]:
            st.markdown("".join(f'<span class="claim-pill">{c}</span>' for c in meta["key_claims"]), unsafe_allow_html=True)
            st.write("")

        if result["warnings"]:
            for w in result["warnings"]:
                st.warning(w)

        # --- Live analysis using the deterministic engines ---
        flags = check_compatibility(r_flat_df, incompat_data)
        ph, _ = estimate_ph(r_flat_df, candidate_df)
        visc = estimate_viscosity(r_flat_df, candidate_df)
        stability_score, stability_notes = estimate_stability(r_flat_df, candidate_df, flags)
        cost_result = calculate_cost(r_flat_df, candidate_df, batch_size_kg)
        reg_results = check_regulatory(r_flat_df, candidate_df, result["region"])
        reg_verdict = summarize(reg_results)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Estimated pH", ph if ph is not None else "n/a")
        render_texture_block(m2, visc["texture_estimate"])
        m3.metric("Stability score", f"{stability_score}/100")
        m4.metric(f"Batch cost ({batch_size_kg:g} kg)", fmt(cost_result["total_cost_usd"]))

        rc1, rc2 = st.columns([2, 1])
        with rc1:
            st.markdown("**Formula (grouped by phase)**")
            for p in phases:
                st.markdown(f"*{p['phase_name']}*")
                p_df = pd.DataFrame(p["ingredients"])
                src_map = candidate_df.set_index("inci_name")["source"].to_dict()
                p_df["source"] = p_df["inci_name"].map(src_map)
                p_df["grams_per_batch"] = (p_df["percent"] / 100 * batch_size_kg * 1000).round(2)
                st.dataframe(
                    p_df[["inci_name", "role", "percent", "grams_per_batch", "source"]].rename(columns={
                        "inci_name": "Ingredient", "role": "Role", "percent": "%", "grams_per_batch": "g / batch", "source": "Source",
                    }),
                    width="stretch", hide_index=True,
                )
        with rc2:
            verdict_map = {
                "compliant": ("🟢 Compliant", "success"), "needs_review": ("🟡 Needs review", "warning"),
                "non_compliant": ("🔴 Non-compliant", "error"),
            }
            label, kind = verdict_map[reg_verdict]
            st.markdown(f"**Regulatory ({result['region']})**")
            getattr(st, kind)(label)

            st.markdown("**Compatibility**")
            if not flags:
                st.success("No known conflicts")
            else:
                for f in sort_flags(flags):
                    st.markdown(f"{severity_badge(f['severity'])} {f['a']} + {f['b']}", unsafe_allow_html=True)

            if cost_result["missing_cost"]:
                st.caption(f"⚠️ No cost on file: {', '.join(cost_result['missing_cost'])}")

        with st.expander("📖 Positioning & sourcing rationale"):
            st.write(f"**Positioning:** {meta['positioning_rationale']}")
            st.write(f"**Sourcing:** {meta['sourcing_rationale']}")
            st.write(f"**Formulation notes:** {meta['formulation_notes']}")
            if result.get("refinement_history"):
                st.write("**Refinement history applied to reach this version:**")
                for i, r in enumerate(result["refinement_history"], 1):
                    st.write(f"{i}. {r}")

        st.markdown("**🔬 Technical product profile**")
        tech_profile = compute_technical_profile(r_flat_df, candidate_df, result["product_category"], result["product_subtype"])
        tech_description = render_technical_description(tech_profile, result["product_category"], result["product_subtype"])
        st.markdown(f'<div class="result-card">{tech_description}</div>', unsafe_allow_html=True)
        st.caption("Computed directly from this formula's actual ingredients/percentages - not AI-generated, so it can't drift from what's really in the formula.")

        with st.expander("📋 Full technical breakdown"):
            tp1, tp2 = st.columns(2)
            with tp1:
                st.markdown("**Active ingredients**")
                if tech_profile["active_ingredients"]:
                    st.dataframe(pd.DataFrame(tech_profile["active_ingredients"]).rename(
                        columns={"name": "Ingredient", "percent": "%", "function": "Function"}),
                        width="stretch", hide_index=True)
                else:
                    st.caption("None")
                st.markdown("**Preservation system**")
                pres = tech_profile["preservation"]
                if pres["preservatives"]:
                    st.dataframe(pd.DataFrame(pres["preservatives"]).rename(columns={"name": "Ingredient", "percent": "%"}),
                                  width="stretch", hide_index=True)
                    st.caption(f"Combined: {pres['total_percent']:g}% · Antioxidant present: {'Yes' if pres['has_antioxidant'] else 'No'} · Chelator present: {'Yes' if pres['has_chelator'] else 'No'}")
                else:
                    st.caption("None detected")
            with tp2:
                st.markdown("**Emulsion / product type**")
                st.write(tech_profile["emulsion_type"])
                if tech_profile["uv_filter"]:
                    st.markdown("**UV filter analysis**")
                    uv = tech_profile["uv_filter"]
                    if uv["filters"]:
                        st.dataframe(pd.DataFrame(uv["filters"]).rename(columns={"name": "Filter", "percent": "%"}),
                                      width="stretch", hide_index=True)
                    st.write(f"**Total: {uv['total_percent']:g}%** — {uv['tier_label']}")
                    st.warning(uv["disclaimer"])
                st.markdown("**Functional ingredients**")
                fi = tech_profile["functional_ingredients"]
                st.caption(f"Emulsifiers: {', '.join(fi['emulsifiers']) or 'none'}")
                st.caption(f"Thickeners: {', '.join(fi['thickeners']) or 'none'}")

        web_ingredients = result.get("web_ingredients") or []
        web_note = result.get("web_note")
        if web_ingredients:
            with st.expander(f"🔍 {len(web_ingredients)} worldwide ingredient(s) found via live web search", expanded=False):
                st.caption("AI-researched from current web sources - costs are estimates, not locked supplier quotes. Verify before sourcing.")
                web_df = pd.DataFrame(web_ingredients).rename(columns={
                    "inci_name": "Ingredient", "category": "Category", "function": "Function",
                    "estimated_cost_per_kg_usd": "Est. Cost/kg (USD)", "sourcing_note": "Sourcing note",
                    "source_confidence": "Confidence",
                })
                st.dataframe(web_df, width="stretch", hide_index=True)
        elif web_note:
            st.caption(f"ℹ️ {web_note}")

        if meta["recommended_worldwide_upgrades"]:
            st.markdown("**✨ Recommended worldwide upgrades for this positioning**")
            up_cols = st.columns(len(meta["recommended_worldwide_upgrades"]))
            for col, u in zip(up_cols, meta["recommended_worldwide_upgrades"]):
                with col:
                    cost_str = f"{fmt(u['cost_per_kg_usd'])}/kg" if u.get("cost_per_kg_usd") else "cost n/a"
                    st.markdown(
                        f"""<div class="result-card"><b>{u['inci_name']}</b><br>
                        <span style="color:#888;font-size:0.85rem;">{cost_str}</span>
                        <p style="font-size:0.9rem;">{u['reason']}</p></div>""",
                        unsafe_allow_html=True,
                    )

        st.divider()
        st.markdown("**Unit economics**")
        u1, u2, u3, u4 = st.columns(4)
        unit_fill_g = u1.number_input("Fill size per unit (g)", min_value=1.0, value=50.0, step=5.0, key="unit_fill_g")
        packaging_cost = u2.number_input(f"Packaging cost/unit ({currency_symbol})", min_value=0.0, value=0.30, step=0.05, key="packaging_cost")
        overhead_pct = u3.number_input("Overhead (%)", min_value=0.0, value=15.0, step=1.0, key="overhead_pct")
        markup_mult = u4.number_input("Markup multiplier (optional)", min_value=0.0, value=0.0, step=0.5, key="markup_mult")

        econ = calculate_unit_economics(
            cost_result["cost_per_kg_batch_usd"], unit_fill_g, packaging_cost, overhead_pct,
            markup_mult if markup_mult > 0 else None,
        )
        units_yield = units_from_batch(batch_size_kg, unit_fill_g)
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("Units from this batch", f"{units_yield:,.0f}")
        e2.metric("Formula cost/unit", fmt(econ["formula_cost_per_unit_usd"], 3))
        e3.metric("Total cost/unit", fmt(econ["total_unit_cost_usd"], 3))
        if "suggested_price_at_multiplier_usd" in econ:
            e4.metric(f"Price @ {markup_mult:g}x", fmt(econ["suggested_price_at_multiplier_usd"]))
        st.caption("Markup is a plain calculator on the number you enter, not a pricing recommendation.")

        st.divider()
        st.markdown("**🔁 Refine this formula**")
        st.caption("Tweak your requirements and ask the AI to revise this exact formula - it keeps the current version as context instead of starting over.")
        refine_col1, refine_col2 = st.columns([3, 1])
        with refine_col1:
            refinement_instruction = st.text_area(
                "What should change?",
                placeholder="e.g. Reduce cost by using cheaper emollients, make it fragrance-free, "
                            "boost hydration further, swap to a more premium active for the brightening claim...",
                height=80, key="refinement_instruction",
            )
        with refine_col2:
            st.write("")
            st.write("")
            refine_clicked = st.button("🔁 Refine with AI", width="stretch")

        if refine_clicked:
            if not ai_available:
                st.error("No Gemini API key configured. Add one in the sidebar, or configure Secrets/.env.")
            elif not refinement_instruction.strip():
                st.error("Describe what should change before refining.")
            else:
                refine_candidate_df = get_candidate_df(result["source_strategy"], fx_rate)
                with st.status("Refining... applying your feedback to the formula", expanded=False) as status:
                    try:
                        call_fn = make_call_fn(api_keys, on_fallback=make_status_fallback_callback(status, "Refining..."))
                        refine_candidate_df, web_ingredients, web_note = augment_with_web_search(
                            refine_candidate_df, call_fn, result["source_strategy"],
                            result["product_category"], result["product_subtype"], result["description"],
                            result["positioning"], fx_rate, status,
                        )
                        candidate_ingredients = build_candidate_context(refine_candidate_df)
                        incompat_rules = build_incompat_context(incompat_data)
                        status.update(label="Refining... applying your feedback to the formula")
                        raw = refine_formula(
                            call_fn, meta, phases, result["product_category"], result["product_subtype"],
                            result["description"], result["positioning"], result["source_strategy"], refinement_instruction,
                            candidate_ingredients, incompat_rules, currency_code=currency_code,
                            prior_refinements=result.get("refinement_history", []),
                            on_retry=make_status_retry_callback(status, "Refining..."),
                        )
                        candidate_names = set(refine_candidate_df["inci_name"])
                        worldwide_names = set(worldwide_df["inci_name"])
                        new_flat, new_phases, new_warnings, new_meta = validate_and_normalize(raw, candidate_names, worldwide_names)
                        new_result = {
                            "flat": new_flat, "phases": new_phases, "warnings": new_warnings, "meta": new_meta,
                            "candidate_df": refine_candidate_df, "region": region, "positioning": result["positioning"],
                            "product_category": result["product_category"], "product_subtype": result["product_subtype"],
                            "description": result["description"], "source_strategy": result["source_strategy"],
                            "refinement_history": result.get("refinement_history", []) + [refinement_instruction],
                            "web_ingredients": web_ingredients, "web_note": web_note,
                        }
                        version_num = len(st.session_state.formula_versions) + 1
                        short_note = refinement_instruction.strip()
                        if len(short_note) > 40:
                            short_note = short_note[:40] + "…"
                        st.session_state.formula_versions.append({"label": f"v{version_num} · {short_note}", "result": new_result})
                        st.session_state.active_version_idx = len(st.session_state.formula_versions) - 1
                        st.session_state.ai_formula_result = new_result
                        status.update(label="Formula refined!", state="complete")
                        st.rerun()
                    except AIError as e:
                        status.update(label="Refinement failed", state="error")
                        st.error(str(e))
                    except FormulaGenerationError as e:
                        status.update(label="Refinement failed", state="error")
                        st.error(str(e))

        st.divider()
        act1, act2, act3 = st.columns(3)
        with act1:
            if st.button("➡️ Load into other tabs", width="stretch"):
                st.session_state.formula = flat
                st.toast("Formula loaded - check Compatibility, Properties, Regulatory, Cost, and AI Assistant tabs.", icon="✅")
        with act2:
            if st.button("🆕 Start over (new formula)", width="stretch"):
                st.session_state.ai_formula_result = None
                st.session_state.formula_versions = []
                st.session_state.active_version_idx = None
                st.rerun()
        with act3:
            export_buf = io.BytesIO()
            with pd.ExcelWriter(export_buf, engine="xlsxwriter") as writer:
                export_rows = []
                for p in phases:
                    for ing in p["ingredients"]:
                        export_rows.append({
                            "Phase": p["phase_name"], "Ingredient": ing["inci_name"], "Role": ing["role"],
                            "Percent": ing["percent"], "Grams (this batch)": round(ing["percent"] / 100 * batch_size_kg * 1000, 2),
                        })
                pd.DataFrame(export_rows).to_excel(writer, index=False, sheet_name="Formula")
                pd.DataFrame([{
                    "Formula Name": meta["formula_name"], "Summary": meta["product_summary"],
                    "Positioning": result["positioning"], "Category": f"{result['product_category']} / {result['product_subtype']}",
                    "Batch Size (kg)": batch_size_kg, "Currency": currency_code,
                    "Total Batch Cost": cost_result["total_cost_usd"],
                    "Cost per kg": cost_result["cost_per_kg_batch_usd"], "Estimated pH": ph,
                    "Texture": visc["texture_estimate"], "Stability Score": stability_score,
                    "Formulation Notes": meta["formulation_notes"], "Key Claims": "; ".join(meta["key_claims"]),
                    "Refinements Applied": " | ".join(result.get("refinement_history", [])) or "none",
                }]).T.to_excel(writer, header=False, sheet_name="Summary")
            st.download_button(
                "📥 Download formula (.xlsx)", data=export_buf.getvalue(),
                file_name=f"{meta['formula_name'].replace(' ', '_')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )

# ==========================================================================
# TAB: Manual Builder
# ==========================================================================
with tab_build:
    st.subheader("Build a formula by hand")
    working_df = get_working_ingredients_df(fx_rate)

    with st.expander("➕ Register a new material (manual entry or Excel/CSV import)"):
        reg_tab1, reg_tab2 = st.tabs(["Type it in", "Import file"])
        with reg_tab1:
            with st.form("manual_material_form", clear_on_submit=True):
                mf1, mf2, mf3 = st.columns(3)
                m_name = mf1.text_input("Material name*")
                m_category = mf2.text_input("Category")
                m_function = mf3.text_input("Function")
                mf4, mf5, mf6 = st.columns(3)
                m_cost = mf4.number_input(f"Cost/kg ({currency_symbol})*", min_value=0.0, step=0.1)
                m_ph_min = mf5.number_input("pH Min (optional)", min_value=0.0, max_value=14.0, step=0.1, value=0.0)
                m_ph_max = mf6.number_input("pH Max (optional)", min_value=0.0, max_value=14.0, step=0.1, value=0.0)
                m_notes = st.text_input("Notes")
                submitted = st.form_submit_button("Add material", type="primary")
                if submitted:
                    if not m_name.strip():
                        st.error("Material name is required.")
                    else:
                        st.session_state.inhouse_df = add_manual_material(
                            st.session_state.inhouse_df, m_name, m_category, m_function, m_cost,
                            ph_min=m_ph_min or None, ph_max=m_ph_max or None, notes=m_notes,
                        )
                        st.success(f"Added \"{m_name}\" - it now appears in the ingredient dropdown below.")
                        st.rerun()
        with reg_tab2:
            manual_upload = st.file_uploader("Upload material list (.xlsx or .csv)", type=["xlsx", "xls", "csv"], key="manual_builder_uploader")
            if manual_upload is not None:
                try:
                    parsed_df, parse_warnings = parse_inhouse_upload(manual_upload)
                    st.session_state.inhouse_df = merge_inhouse_upload(st.session_state.inhouse_df, parsed_df)
                    st.success(f"Merged {len(parsed_df)} material(s) from {manual_upload.name}.")
                    for w in parse_warnings:
                        st.warning(w)
                    st.rerun()
                except InHouseParseError as e:
                    st.error(str(e))
            st.caption("Same importer as Formula Studio - materials you add here are available everywhere in the app.")

    col1, col2 = st.columns([2, 1])
    with col1:
        all_names = sorted(working_df["inci_name"].tolist())
        new_ingredient = st.selectbox("Quick-add a known ingredient", all_names, key="add_ingredient_select")
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
        st.caption("Ingredient names below are free-text - type any name directly, or use the quick-add dropdown above for known materials.")
        edited = st.data_editor(
            formula_df(),
            num_rows="dynamic",
            column_config={
                "inci_name": st.column_config.TextColumn("Ingredient (INCI or material name)", required=True),
                "percent": st.column_config.NumberColumn("Percent (%)", min_value=0.0, max_value=100.0, step=0.1, required=True),
            },
            key="formula_editor",
            width="stretch",
        )
        st.session_state.formula = edited.to_dict("records")

        total_pct = sum(r["percent"] for r in st.session_state.formula)
        if abs(total_pct - 100) > 0.05:
            st.warning(f"Total is {total_pct:.2f}% — adjust so the formula sums to 100% before relying on the estimates in other tabs.")
        else:
            st.success("Formula sums to 100%.")

        unknown_names = [r["inci_name"] for r in st.session_state.formula if r["inci_name"] not in set(working_df["inci_name"])]
        if unknown_names:
            st.info(f"Not yet in your ingredient database (cost/property lookups will show as unavailable): {', '.join(unknown_names)}. Register them above to get full calculations.")
    else:
        st.info("Add ingredients above to start building a formula.")

    with st.expander("📖 View full ingredient database (worldwide + your in-house materials)"):
        st.dataframe(working_df, width="stretch")

# ==========================================================================
# TAB: Compatibility
# ==========================================================================
with tab_compat:
    st.subheader("Ingredient compatibility check")
    if not st.session_state.formula:
        st.info("Add ingredients in the Manual Builder or generate a formula in Formula Studio first.")
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

# ==========================================================================
# TAB: Properties
# ==========================================================================
with tab_props:
    st.subheader("Estimated physical properties")
    working_df = get_working_ingredients_df(fx_rate)
    if not st.session_state.formula:
        st.info("Add ingredients in the Manual Builder or generate a formula in Formula Studio first.")
    else:
        fdf = formula_df()
        flags = check_compatibility(fdf, incompat_data)

        ph, ph_contributors = estimate_ph(fdf, working_df)
        visc = estimate_viscosity(fdf, working_df)
        stability_score, stability_notes = estimate_stability(fdf, working_df, flags)

        c1, c2, c3 = st.columns(3)
        c1.metric("Estimated pH", ph if ph is not None else "n/a")
        render_texture_block(c2, visc["texture_estimate"])
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
                st.dataframe(contrib_df, width="stretch")
            else:
                st.write("No ingredients in this formula have a defined pH range.")

        with st.expander("How the texture/viscosity estimate was calculated"):
            st.write(f"Water phase: {visc['water_phase_percent']}% · Oil phase: {visc['oil_phase_percent']}%")
            if visc["contributing_ingredients"]:
                vc_df = pd.DataFrame(visc["contributing_ingredients"], columns=["Ingredient", "Category", "Thickening contribution"])
                st.dataframe(vc_df, width="stretch")

        st.caption("Heuristic estimates for directional R&D use only - always confirm with a calibrated pH meter, viscometer, and a real stability protocol (accelerated aging, freeze-thaw, centrifuge).")

        st.divider()
        st.markdown("**🔬 Technical product profile**")
        tech_profile = compute_technical_profile(fdf, working_df, "", "")
        tech_description = render_technical_description(tech_profile, "", "")
        st.markdown(f'<div class="result-card">{tech_description}</div>', unsafe_allow_html=True)
        st.caption("Computed directly from this formula's actual ingredients/percentages.")

# ==========================================================================
# TAB: Regulatory
# ==========================================================================
with tab_reg:
    st.subheader(f"Regulatory check — {region}")
    working_df = get_working_ingredients_df(fx_rate)

    with st.expander("📚 Regulatory methodology, assumptions & what's covered", expanded=not st.session_state.formula):
        st.markdown(f"**Framework referenced for {region}:** {REGION_METHODOLOGY[region]['framework']}")
        st.markdown("**Regions considered in this tool:** EU, US, and India - selectable in the sidebar. Each region has its own independent allow/limit data; nothing is extrapolated across regions automatically.")
        st.markdown("**Assumptions specific to this region:**")
        for a in REGION_METHODOLOGY[region]["assumptions"]:
            st.markdown(f"- {a}")
        st.markdown("**What a status icon means:**")
        for status_key, (icon, explanation) in STATUS_LEGEND.items():
            st.markdown(f"- {icon} **{status_key}** — {explanation}")
        st.markdown(
            "**Scope limits:** this check only evaluates ingredient permission and maximum "
            "concentration for the ingredients present in the current sample database. It does "
            "NOT check labeling requirements, allergen declarations, claims substantiation, "
            "packaging/safety-assessment documentation, or any region not listed above."
        )
        st.error(
            "⚠️ This is a formulation-assistance tool, not legal or regulatory advice. The "
            "underlying dataset is a small curated sample, not a live/complete regulatory feed. "
            "A qualified regulatory affairs professional must review and sign off before any "
            "commercial claim or sale."
        )

    if not st.session_state.formula:
        st.info("Add ingredients in the Manual Builder or generate a formula in Formula Studio first.")
    else:
        results = check_regulatory(formula_df(), working_df, region)
        verdict = summarize(results)
        verdict_map = {
            "compliant": ("🟢 Compliant (per sample data)", "success"),
            "needs_review": ("🟡 Needs manual review", "warning"),
            "non_compliant": ("🔴 Non-compliant (per sample data)", "error"),
        }
        label, kind = verdict_map[verdict]
        getattr(st, kind)(label)

        res_df = pd.DataFrame(results)
        res_df["icon"] = res_df["status"].map(lambda s: STATUS_LEGEND.get(s, ("", ""))[0])
        res_df = res_df.rename(columns={
            "inci_name": "Ingredient", "percent": "% in formula", "icon": "", "status": "Status", "message": "Regulatory note / basis",
        })[["", "Status", "Ingredient", "% in formula", "Regulatory note / basis"]]
        st.dataframe(res_df, width="stretch", hide_index=True)

        n_unknown = sum(1 for r in results if r["status"] == "unknown")
        if n_unknown:
            st.info(f"{n_unknown} ingredient(s) have no regulatory data on file (see the methodology panel above) - these need manual verification before use.")

# ==========================================================================
# TAB: Cost & Sustainability
# ==========================================================================
with tab_cost:
    st.subheader("Cost breakdown & greener/cheaper swaps")
    working_df = get_working_ingredients_df(fx_rate)
    if not st.session_state.formula:
        st.info("Add ingredients in the Manual Builder or generate a formula in Formula Studio first.")
    else:
        cost_result = calculate_cost(formula_df(), working_df, batch_size_kg)

        c1, c2 = st.columns(2)
        c1.metric(f"Total batch cost ({batch_size_kg:g} kg)", fmt(cost_result["total_cost_usd"]))
        c2.metric("Cost per kg", fmt(cost_result["cost_per_kg_batch_usd"], 4))

        line_df = pd.DataFrame(cost_result["line_items"]).sort_values("line_cost_usd", ascending=False, na_position="last")
        display_line_df = line_df.rename(columns={
            "inci_name": "Ingredient", "percent": "%", "kg_used": "kg used",
            "cost_per_kg_usd": f"Cost/kg ({currency_symbol})", "line_cost_usd": f"Line cost ({currency_symbol})",
            "sustainability_score": "Sustainability",
        })
        st.dataframe(display_line_df, width="stretch")
        chart_df = line_df.dropna(subset=["line_cost_usd"])
        if not chart_df.empty:
            st.bar_chart(chart_df.set_index("inci_name")["line_cost_usd"])

        if cost_result["missing_from_db"]:
            st.warning(f"Not in database: {', '.join(cost_result['missing_from_db'])}")
        if cost_result["missing_cost"]:
            st.warning(f"No cost on file (excluded from total): {', '.join(cost_result['missing_cost'])}")

        if currency_code != "USD":
            st.caption(f"Worldwide-database costs converted from USD at 1 USD = {fx_rate:g} {currency_code} (editable in the sidebar). In-house costs are used exactly as you entered them.")

        st.divider()
        st.markdown("**Substitute suggestions** (same function, cheaper and/or more sustainable)")
        target = st.selectbox("Find alternatives for", [r["inci_name"] for r in st.session_state.formula])
        subs = find_substitutes(target, working_df)
        if subs:
            subs_df = pd.DataFrame(subs).rename(columns={
                "inci_name": "Ingredient", "cost_per_kg_usd": f"Cost/kg ({currency_symbol})",
                "cost_delta_usd_per_kg": f"Savings/kg ({currency_symbol})",
                "sustainability_score": "Sustainability", "sustainability_delta": "Sustainability Δ",
            })
            st.dataframe(subs_df, width="stretch")
        else:
            st.info("No same-function alternatives found in the current database for this ingredient.")

# ==========================================================================
# TAB: AI Assistant
# ==========================================================================
with tab_ai:
    st.subheader("Ask the AI formulation assistant")
    st.caption("AI-powered narrative layer. It reasons over the numbers computed in the other tabs - it does not invent new regulatory limits.")
    working_df = get_working_ingredients_df(fx_rate)

    if not ai_available:
        st.info("No Gemini API key configured. Add one in the sidebar, or configure Secrets/.env, to enable this tab.")
    elif not st.session_state.formula:
        st.info("Add ingredients in the Manual Builder or generate a formula in Formula Studio first.")
    else:
        fdf = formula_df()
        flags = check_compatibility(fdf, incompat_data)
        ph, _ = estimate_ph(fdf, working_df)
        visc = estimate_viscosity(fdf, working_df)
        stability_score, stability_notes = estimate_stability(fdf, working_df, flags)
        reg_results = check_regulatory(fdf, working_df, region)
        cost_result = calculate_cost(fdf, working_df, batch_size_kg)

        context_blob = {
            "formula": st.session_state.formula,
            "estimated_ph": ph,
            "texture_estimate": visc["texture_estimate"],
            "stability_score": stability_score,
            "stability_notes": stability_notes,
            "compatibility_flags": flags,
            "regulatory_region": region,
            "regulatory_results": reg_results,
            "currency": currency_code,
            "total_cost": cost_result["total_cost_usd"],
            "batch_size_kg": batch_size_kg,
        }

        default_question = "Review this formula. Call out any red flags, and suggest 1-2 concrete improvements for stability, cost, or sustainability."
        user_question = st.text_area("Your question", value=default_question, height=100)

        if st.button("🤖 Ask AI"):
            prompt = (
                f"Computed formulation data (JSON, costs in {currency_code}):\n{json.dumps(context_blob, indent=2)}\n\n"
                f"Chemist's question: {user_question}"
            )
            with st.status("Thinking...", expanded=False) as status:
                try:
                    call_fn = make_call_fn(api_keys, on_fallback=make_status_fallback_callback(status, "Thinking..."))
                    reply = call_fn(prompt, on_retry=make_status_retry_callback(status, "Thinking..."))
                    status.update(label="Done", state="complete")
                    st.markdown(reply)
                except AIError as e:
                    status.update(label="Failed", state="error")
                    st.error(str(e))
