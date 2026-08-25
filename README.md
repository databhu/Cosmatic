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
cp .env.example .env   # then edit .env and add your Gemini key - see below
streamlit run app.py
```

Then open the local URL Streamlit prints (usually http://localhost:8501).
The `.env` step is optional - skip it and you can still paste your key into
the sidebar once the app is open.

## AI provider: Google Gemini (dual-key, fully automatic)

The app uses **Google Gemini** exclusively, with a genuinely free tier (no
credit card required). There's no model to pick and no provider to choose -
just add one or two API keys, and everything else is automatic:

- The app always tries **gemini-3.5-flash-lite** first (confirmed to work
  well) and automatically switches to another Gemini model on its own if
  that one is ever deprecated, incompatible with the request, rate-limited,
  or the servers are overloaded - no dropdown, no user action.
- If you configure a **second key**, the app automatically switches to it
  if the first is exhausted or rejected - again, no action needed when it
  happens.

**You have three ways to supply each key:**

**Option A - `.env` file (recommended for local dev)**
```bash
cp .env.example .env
# then edit .env and paste your key(s):
# GEMINI_API_KEY=AIza...
# GEMINI_API_KEY_2=AIza...   (optional backup)
```
Get a free key (no credit card required) at https://aistudio.google.com/apikey.
The app loads `.env` automatically via `python-dotenv` on startup. `.env` is
already in `.gitignore` - it will never get committed.

**Option B - paste directly into the sidebar** each time you run the app
(password-type fields, never written to disk). The "Gemini API key(s)"
expander has a field for the primary key and an optional backup.

**Option C - Streamlit Community Cloud Secrets** (for the hosted version -
see the deploy section below). Add `GEMINI_API_KEY` (and optionally
`GEMINI_API_KEY_2`) there if you want it available to visitors without them
needing their own.

Priority per key if more than one source is set: **Streamlit Secrets >
`.env`/environment variable > blank**.

**The sidebar never displays a configured secret.** If a key is set via
Secrets or `.env`, its field just shows a "✅ Configured" confirmation -
never the key itself, even in a password field. A checkbox lets you type a
*different* key for just that session if you want to (e.g. testing your own
key against a shared deployment) without ever seeing or overwriting the
configured one.

Manual formula building, compatibility rules, property estimates, regulatory
check, and cost calculator all work with **no API key configured at all**.
AI is only needed for Formula Studio's generation/refinement and the AI
Assistant chat tab.

### Automatic fallback: how it actually decides to switch

Every AI call automatically retries on rate limits (HTTP 429) and transient
server errors (500/502/503/504) or timeouts, using exponential backoff with
jitter and honoring Gemini's `Retry-After` header when it sends one. Rate
limits get a *smaller* retry budget than genuine server hiccups, since a
rate limit rarely clears within seconds - it's faster and more effective to
move on than to keep waiting on the same model.

Beyond retrying, there are three distinct triggers for switching to a
*different* model or key entirely, all fully automatic:

1. **Deprecated/unavailable model (404)** - tries the next model on the
   same key.
2. **Persistent rate limit or server overload (429/503, even after
   retrying)** - tries the next model on the same key.
3. **"Other generation issues"** - the model responded successfully (200
   OK) but produced content that isn't actually usable (e.g. not valid JSON
   for a formula request). The app validates every AI response before
   trusting it, and treats an unusable response as a failure just like a
   network error - automatically trying the next model rather than
   accepting garbage. A same-model retry is attempted first (in case it was
   a one-off slip), then it moves on.

An invalid/rejected key (401/403) skips straight to the *second key*
(if configured) rather than wasting attempts on sibling models with the
same bad key. You'll see live status messages the whole way (e.g. "Gemini
(gemini-3.5-flash-lite) unavailable, switching to Gemini
(gemini-3.6-flash)..."). If every model on every configured key fails, you
get one clear error message, never a raw stack trace.

**Model IDs go stale fast.** Google retired `gemini-2.0-flash` on
2026-03-31 (about 8 months after release) and had shipped four more Flash
generations by August 2026. If you start seeing repeated failures, open
`utils/ai_client.py`, find the `GEMINI_MODELS` list near the top, and
update it with current model IDs from
https://ai.google.dev/gemini-api/docs/models - nothing else needs to
change, every part of the app references that list dynamically.

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
- **Live web search for worldwide ingredients** is built but currently
  dormant: the original implementation relied on Groq's search-capable
  models, which have been removed along with Groq itself. The app
  gracefully falls back to the static database with an explanatory caption.
  See `utils/ai_client.py`'s `WEB_SEARCH_CAPABLE_MODELS` (currently empty)
  if you want to investigate re-enabling this via a confirmed
  search-capable Gemini model.

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

**Technical product profile** - every generated formula includes a technical
breakdown computed straight from the formula's real ingredients/percentages:
active ingredients, emulsion/product type, preservation system, and (for
sunscreens, or any formula containing mineral UV filters) combined UV filter
loading. This is deliberately **not** AI-generated - SPF is a regulated,
lab-tested claim (FDA OTC monograph / ISO 24444), so rather than let an AI
guess a plausible-sounding number, the app reports the real UV filter
percentage and maps it to a heavily-caveated directional tier ("in the range
commonly used for ~SPF 30 mineral formulations") with a loud disclaimer that
it is not a substitute for actual testing. The same technical profile also
appears in the Properties tab for manually-built formulas.

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
assets/cosmogen_icon.png       Square icon at higher resolution (512x512) -
                                spare brand asset, not currently rendered
                                in-app (the favicon uses a smaller copy)
assets/cosmogen_wordmark.png   Icon + "CosmoGen" wordmark, cropped from the
                                original lockup - embedded in the hero banner
data/ingredients.csv           ~45 worldwide cosmetic ingredients: category,
                                cost/kg (USD), sustainability score, typical pH
                                range, and EU/US/India allowed status + limits
data/incompatibilities.json    Known ingredient-pair interaction rules
utils/ai_client.py             Gemini-only AI client: dual-key + automatic
                                model fallback (deprecated models, rate
                                limits, and unusable-content all trigger
                                switching), retry/backoff on transient errors
utils/formula_ai.py            AI formula generation + refinement + live web
                                search for worldwide ingredients: prompt
                                building, strict JSON validation, hallucination
                                filtering, percentage renormalization
utils/inhouse_materials.py     Excel/CSV upload parsing, template generator,
                                in-house + worldwide merge logic
utils/currency.py              Currency options, USD conversion, formatting
utils/property_estimator.py    Deterministic pH / viscosity / stability heuristics
utils/compatibility_checker.py Rule-based pairwise incompatibility lookup
utils/regulatory_checker.py    Region-by-region allowed/limit checking
utils/cost_calculator.py       Batch costing, unit economics, substitute finder
utils/technical_profile.py     Deterministic technical product profile (active
                                ingredients, UV filter analysis, emulsion type,
                                preservation system) - no AI involved
```

## Branding

The app's palette is sampled directly from the CosmoGen logo: a purple→cyan
gradient (`#8154FC` → `#4ADAFD`) on a deep navy background (`#05061C` →
`#0D1250`). Two assets from the actual provided artwork are used:

- `assets/cosmogen_favicon.png` (the square icon) - browser-tab favicon.
- `assets/cosmogen_wordmark.png` (icon + "CosmoGen" text, cropped from the
  original lockup) - embedded directly in the hero banner. "Cosmo" is
  rendered in near-white in the source art, designed to sit on a dark
  surface, which is why it's placed on the hero banner's dark navy
  background rather than anywhere with a light background.
- The lockup's own baked-in tagline text was excluded from the crop (too
  thin to stay legible at hero-banner scale) - the tagline shown next to the
  logo ("AI Cosmetic Formulation Studio") is separately rendered text
  instead, sized for readability.

If the `assets/` folder is ever missing (e.g. accidentally left out of a
deploy), the app falls back to a plain "CosmoGen" text heading rather than
breaking. To swap in different brand assets later, replace the PNGs in
`assets/` and adjust the gradient stops in the `CUSTOM_CSS` block near the
top of `app.py` (commented) to match.


### Design principle: AI proposes, the app disposes

All the numbers that actually matter for safety/compliance/cost - regulatory
limits, pH ranges, ingredient prices, incompatibility flags - come from the
local CSV/JSON data and plain Python math, **not** from the LLM. In Formula
Studio, the AI is only allowed to pick ingredients from the exact candidate
list it's handed (so cost/availability is always real), and every formula it
returns is re-validated in plain Python: hallucinated ingredient names are
stripped with a visible warning, and percentages are rescaled to sum to
exactly 100%. In the AI Assistant tab, Gemini is only called to add qualitative
interpretation on top of numbers the app already computed, and is explicitly
told not to invent new regulatory figures. This keeps the tool useful
without inheriting an LLM's hallucination risk on the facts that matter most.

## Mobile support

The app is responsive down to phone-sized screens (tested at 375px/iPhone
width): the hero banner stacks the logo above the text instead of cramming
them side by side, buttons go full-width with touch-friendly tap targets
(44px minimum), headings and metric numbers scale down so they don't
dominate a small screen, form inputs use 16px text (avoids iOS's
auto-zoom-on-focus), and tables scroll horizontally instead of squashing
columns unreadably. Streamlit's own layout (columns, containers) already
stacks responsively on narrow viewports by default; the custom CSS in
`app.py`'s `CUSTOM_CSS` block (look for the `@media` blocks near the bottom)
handles the elements this app adds on top of that - the hero banner, badges,
result cards, and pills.

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
  or a database by this app. It IS sent to Google's Gemini API as part of the
  prompt whenever you generate an AI formula, so don't upload data you
  wouldn't want leaving your infrastructure under Google's data-handling
  terms (see the free-tier note in the AI provider section above - free-tier
  prompts may be used to improve Google's products). If the app is hosted
  publicly (see below), remember uploaded data only persists for that
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

**Optional - bake in shared key(s)** so visitors don't need their own:
in the app's Streamlit Cloud dashboard go to **Settings → Secrets** and add:
```toml
GEMINI_API_KEY = "AIza..."
GEMINI_API_KEY_2 = "AIza..."   # optional backup
```
`app.py` already checks `st.secrets` for these and shows a "✅ Configured"
confirmation for each (visitors can still override with their own key via
the checkbox). Without this, each visitor just pastes their own key(s) in
the sidebar - nothing is stored server-side either way. Note that `.env` is
a local-only mechanism (it's gitignored, so it never reaches GitHub or
Streamlit Cloud) - on Cloud, Secrets is the equivalent.

**Note on public hosting**: since the app is public, anyone with the URL can
use it (and, if you set a shared secret key, can spend your Gemini quota via
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
- **Add another AI provider**: `utils/ai_client.py` currently targets
  Gemini's OpenAI-compatible endpoint specifically; the same request/retry
  pattern works for any OpenAI-compatible endpoint (Groq, OpenAI itself,
  etc.) if you want to reintroduce multi-provider support later.
