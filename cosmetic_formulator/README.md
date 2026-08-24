# CosmoGen — AI Cosmetic Formulation Studio

A Streamlit R&D tool for cosmetic formulators. The centerpiece is **Formula
Studio**: upload your in-house material costs, pick a sourcing strategy and
a positioning tier, describe the product you want, and get a complete
AI-designed formula - phased, costed, property-estimated, and checked
against a sample regulatory/compatibility ruleset - in one pass. The rest of
the app (manual builder, compatibility, properties, regulatory, cost) works
standalone too, and any AI-generated formula can be sent into them for
deeper analysis.

Defaults out of the box: **₹ INR** currency, **India** regulatory region,
and a **1 kg** trial batch size - all editable in the sidebar.

## Run it

```bash
cd cosmetic_formulator
python -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
cp .env.example .env   # then edit .env and add your Groq key - see below
streamlit run app.py
```

Then open the local URL Streamlit prints (usually http://localhost:8501).
The `.env` step is optional - skip it and you can still paste your key into
the sidebar once the app is open.

## Groq API key

You have three ways to give the app your key - use whichever fits how you're running it:

**Option A - `.env` file (recommended for local dev)**
```bash
cp .env.example .env
# then edit .env and paste your key:
# GROQ_API_KEY=gsk_...
```
Get a free key at https://console.groq.com. The app loads `.env` automatically
via `python-dotenv` on startup and pre-fills the sidebar with it. `.env` is
already in `.gitignore` - it will never get committed.

**Option B - paste it directly into the sidebar** each time you run the app
(a password-type field, never written to disk).

**Option C - Streamlit Community Cloud Secrets** (for the hosted version -
see the deploy section below).

Priority if more than one is set: **Streamlit Secrets > `.env`/environment
variable > blank**. You can also optionally set `GROQ_MODEL` in `.env` to
preselect a default model in the sidebar dropdown (must match one of the IDs
in `utils/groq_client.py`'s `AVAILABLE_MODELS`).

**The sidebar never displays a configured secret.** If `GROQ_API_KEY` is set
via Secrets or `.env`, the sidebar just shows a "✅ configured" confirmation -
never the key itself, even in a password field. A checkbox lets you type a
*different* key for just that session if you want to (e.g. testing your own
key against a shared deployment) without ever seeing or overwriting the
configured one. If nothing is configured anywhere, the sidebar shows a clear
warning explaining how to add one instead of silently failing later.

Manual formula building, compatibility rules, property estimates, regulatory
check, and cost calculator all work with **no API key at all**. The Groq key
only unlocks Formula Studio's AI generation and the AI Assistant chat tab.

### Rate limits and transient errors

Every Groq call automatically retries on rate limits (HTTP 429) and transient
server errors (500/502/503/504) or timeouts, using exponential backoff with
jitter and honoring Groq's `Retry-After` header when it sends one (up to 4
retries, capped at 20s between attempts). You'll see the status message
update live (e.g. "retrying in 3s...") rather than a frozen spinner. If Groq
is still rate-limiting after all retries, you get a clear one-line message
telling you to wait and try again - never a raw stack trace.

## Formula Studio walkthrough

**Step 1 - Material sourcing**
- Upload an in-house material list (`.xlsx`/`.csv`) or use the "Download
  template" button for the expected columns. Only *Material Name* and
  *Cost/kg* are required; category, function, pH range, sustainability
  score, stock, and notes are optional but improve the AI's formulation
  quality. You can also add/edit rows directly in the in-app table.
  Re-uploading merges into what you already have (matching names get
  updated, everything else is kept) rather than wiping the table.
- Choose a sourcing strategy: **In-House**, **Worldwide** (the built-in
  ~45-ingredient sample database), or **In-House + Worldwide**. On a name
  collision, your in-house cost/data wins.

**Step 2 - Product brief**
- Pick a product category/type (Skincare, Color Cosmetics, Haircare,
  Personal Care, each with sub-types) and a positioning tier (Budget /
  Mid-Range / Premium).
- Describe what you want in plain language (skin type, texture, key actives,
  claims, anything to avoid).

**Step 3 - Generate**
- The AI is only allowed to choose ingredients from the exact candidate list
  you gave it (grounded by real cost/source data) and must return structured
  JSON. The app then independently: validates every ingredient name against
  the real candidate list (dropping anything hallucinated, with a visible
  warning), rescales percentages to sum to exactly 100%, and re-runs the same
  deterministic pH/viscosity/stability, compatibility, regulatory, and cost
  engines used elsewhere in the app on the result - the AI proposes, the app
  disposes.
- Results show phased ingredient tables with gram quantities for your batch
  size, live property/compatibility/regulatory checks, unit economics
  (cost/unit, batch yield, optional packaging/overhead/markup calculator, all
  in your selected currency), and - regardless of your sourcing strategy - a
  short list of worldwide ingredients that could elevate the formula further
  for the chosen positioning.

**Refine, don't restart**
- Below the results, describe what should change ("make it fragrance-free",
  "cut cost using cheaper emollients", "add a stronger brightening active")
  and click **Refine with AI**. The request goes back to the model *together
  with the current formula as context*, so it revises rather than starting
  from a blank slate. Each refinement becomes a new version (v1, v2, ...)
  with a version picker above the results so you can compare or branch off
  an earlier version instead of the latest one.
- **Load into other tabs** copies whichever version you're viewing into the
  Manual Builder / Compatibility / Properties / Regulatory / Cost / AI
  Assistant tabs for further editing or a deeper AI conversation.
  **Download formula (.xlsx)** exports a spec sheet including the refinement
  history. **Start over (new formula)** clears everything, including version
  history, for a genuinely fresh attempt.

## Currency

Pick a currency in the sidebar (USD, EUR, GBP, INR, JPY, AUD, CAD, CNY, AED).
The built-in worldwide ingredient database is authored in USD, so non-USD
currencies convert it using an exchange rate shown next to the currency
picker - a reasonable illustrative default that **you should overwrite with
today's actual rate** for real accuracy; it's not a live feed. Your in-house
material costs are used exactly as you entered them (no conversion applied),
since those are your own real costs in whatever currency you already track
them in. The selected currency applies everywhere: Formula Studio, Manual
Builder, Cost & Sustainability, unit economics, and the Excel export.

## How it's built

```
app.py                        Streamlit UI - Formula Studio + 6 supporting tabs
assets/cosmogen_favicon.png    Browser-tab favicon (64x64)
assets/cosmogen_icon.png       App icon used in the hero banner (512x512)
assets/cosmogen_wordmark.png   Full logo lockup (icon + wordmark + tagline) -
                                kept as a brand asset; not rendered in-app since
                                the in-app title uses CSS gradient text instead
                                (see "Branding" below)
data/ingredients.csv           ~45 worldwide cosmetic ingredients: category,
                                cost/kg (USD), sustainability score, typical pH
                                range, and EU/US/India allowed status + limits
data/incompatibilities.json    Known ingredient-pair interaction rules
utils/groq_client.py           Groq chat-completions wrapper: retry/backoff
                                on rate limits & transient errors
utils/formula_ai.py            AI formula generation + refinement: prompt
                                building, strict JSON validation, hallucination
                                filtering, percentage renormalization
utils/inhouse_materials.py     Excel/CSV upload parsing, template generator,
                                in-house + worldwide merge logic
utils/currency.py              Currency options, USD conversion, formatting
utils/property_estimator.py    Deterministic pH / viscosity / stability heuristics
utils/compatibility_checker.py Rule-based pairwise incompatibility lookup
utils/regulatory_checker.py    Region-by-region allowed/limit checking
utils/cost_calculator.py       Batch costing, unit economics, substitute finder
```

## Branding

The app's palette is sampled directly from the CosmoGen logo: a purple→cyan
gradient (`#8154FC` → `#4ADAFD`) on a deep navy background (`#05061C` →
`#0D1250`). The square icon (`assets/cosmogen_icon.png`) is used as both the
browser favicon and the mark in the hero banner; the "CosmoGen" wordmark
itself is rendered as CSS gradient-fill text rather than an embedded image -
the original horizontal lockup has "Cosmo" in near-white, designed to sit on
a dark surface, so recreating it in CSS keeps it crisp and legible at any
size rather than depending on a raster image's fixed proportions. To swap in
different brand colors later, edit the `CUSTOM_CSS` block near the top of
`app.py` (the gradient stops are called out in a comment) and the icon files
in `assets/`.


### Design principle: AI proposes, the app disposes

All the numbers that actually matter for safety/compliance/cost - regulatory
limits, pH ranges, ingredient prices, incompatibility flags - come from the
local CSV/JSON data and plain Python math, **not** from the LLM. In Formula
Studio, the AI is only allowed to pick ingredients from the exact candidate
list it's handed (so cost/availability is always real), and every formula it
returns is re-validated in plain Python: hallucinated ingredient names are
stripped with a visible warning, and percentages are rescaled to sum to
exactly 100%. In the AI Assistant tab, Groq is only called to add qualitative
interpretation on top of numbers the app already computed, and is explicitly
told not to invent new regulatory figures. This keeps the tool useful
without inheriting an LLM's hallucination risk on the facts that matter most.

## Manual Builder

Build a formula ingredient-by-ingredient without the AI. Ingredient names are
free-text - type anything, including materials not yet in your database (a
message flags anything the app can't yet look up cost/property data for, but
never blocks you from entering it). To get full calculations for a new
material, register it via the **"Register a new material"** panel at the top
of the tab, either by typing it in directly or importing an Excel/CSV file -
the same importer Formula Studio uses, so materials you add in either place
are available everywhere in the app immediately.

## Regulatory methodology

The Regulatory tab opens with an expandable panel (auto-expanded if you
haven't built a formula yet) explaining, per selected region: which legal
framework the sample data references (EU 1223/2009 + CosIng, US FDA rules/OTC
monographs, India BIS/CDSCO), the specific assumptions baked into that
region's data, what each status icon (✅/⚠️/⛔/❓) actually means, and an
explicit list of what's *out of scope* (labeling, allergen declarations,
claims substantiation - only ingredient permission/concentration is checked).
This is meant to make the tool's limitations legible rather than just
tucking a disclaimer at the bottom - it's still not a substitute for a
qualified regulatory affairs review.

## ⚠️ Important limitations

- **`data/ingredients.csv` is a small illustrative sample (~45 ingredients),
  not a complete or continuously updated regulatory database.** A few entries
  (e.g. the EU's 2024/996 retinol and alpha-arbutin limits) were checked
  against public sources at the time of writing; most others are reasonable
  placeholders for demo purposes. Before using this for a real product:
  - Replace/extend the CSV with a verified feed from **EU CosIng**
    (Regulation (EC) 1223/2009 + annexes), **US FDA** cosmetic rules/OTC
    monographs, and **India BIS/CDSCO** standards.
  - Have a qualified regulatory affairs professional sign off.
- The compatibility rules are a curated set of well-known interactions, not
  an exhaustive chemical reactivity database.
- pH/viscosity/stability are directional heuristics - always confirm with a
  calibrated pH meter, viscometer, and real stability protocol (accelerated
  aging, freeze-thaw cycling, centrifuge testing).
- Cost figures are illustrative market-rate approximations, not live supplier
  quotes.
- **Exchange rates are static illustrative defaults, not a live feed** - when
  using a non-USD currency, update the rate in the sidebar with today's
  actual value for accurate results.
- **In-house material data (names, costs, stock) is only ever held in the
  browser session's memory (`st.session_state`)** - it's not written to disk
  or a database by this app. It IS sent to Groq's API as part of the prompt
  whenever you generate an AI formula, so don't upload data you wouldn't want
  leaving your infrastructure under Groq's data-handling terms. If the app is
  hosted publicly (see below), remember uploaded data only persists for that
  visitor's session and resets on reload/redeploy - it isn't shared between
  visitors, but it also isn't backed up anywhere.

## Deploy to Streamlit Community Cloud (free hosting)

1. Push this folder's contents to a GitHub repo (repo root = `app.py`,
   `requirements.txt`, `data/`, `utils/`, `assets/` - the favicon and logo
   won't load without the `assets/` folder, though the app still runs fine,
   just with a plain emoji fallback):
   ```bash
   cd cosmetic_formulator
   git init
   git add .
   git commit -m "Cosmetic formulation assistant"
   gh repo create cosmetic-formulator --public --source=. --push
   # or: create an empty repo on github.com, then
   # git remote add origin https://github.com/<you>/cosmetic-formulator.git
   # git push -u origin main
   ```
2. Go to **[share.streamlit.io](https://share.streamlit.io)** and sign in with GitHub.
3. Click **"Create app"** → choose your repo/branch → set **Main file path** to `app.py` → **Deploy**.
4. It builds `requirements.txt` automatically and gives you a public URL like
   `https://<something>.streamlit.app`.

**Optional - bake in a shared Groq key** so visitors don't need their own:
in the app's Streamlit Cloud dashboard go to **Settings → Secrets** and add:
```toml
GROQ_API_KEY = "gsk_..."
```
`app.py` already checks `st.secrets` for this and pre-fills the sidebar field
with it if present (visitors can still override it with their own key).
Without this, each visitor just pastes their own key in the sidebar - nothing
is stored server-side either way. Note that `.env` is a local-only mechanism
(it's gitignored, so it never reaches GitHub or Streamlit Cloud) - on Cloud,
Secrets is the equivalent.

**Note on public hosting**: since the app is public, anyone with the URL can
use it (and, if you set a shared secret key, can spend your Groq quota via
the AI Assistant tab). If you want to restrict access, Community Cloud
supports app-level viewer permissions/private apps on paid tiers - see
[Streamlit's docs](https://docs.streamlit.io/deploy/streamlit-community-cloud).

## Extending it

- **Add ingredients**: append rows to `data/ingredients.csv` following the
  same columns.
- **Add compatibility rules**: append to `pair_rules` in
  `data/incompatibilities.json`.
- **Add a region**: add a new entry to `REGIONS` in `utils/regulatory_checker.py`,
  matching `*_allowed` / `*_max_percent` / `*_notes` columns to the CSV, and
  an entry in `REGION_METHODOLOGY` in `app.py` for the explanation panel.
- **Add a currency**: add an entry to `CURRENCY_OPTIONS` in `utils/currency.py`
  with its symbol, name, and an approximate default USD rate.
- **Add a product category/type**: edit the `PRODUCT_CATEGORIES` dict at the
  top of `app.py`.
- **Swap AI providers**: `utils/groq_client.py` is a ~60-line wrapper; the
  same pattern works for any OpenAI-compatible endpoint.
